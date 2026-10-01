"""PO -> Coder átadás a Supervisorban (accept_patch, code_task) – mock módban, 0 token."""
import json, os, unittest
from factory.rate_limiter import DailyQuotaExhausted, WaitTooLong
from tests.test_master_coder import CoderBase, out, edit, newf, ORIG, IMPL, TEST, GOOD_PATCH
from tests.test_product_owner import good


class HandoffBase(CoderBase):
    def ready(self, spec=None):
        """A Coder-teszt alapjára: PO-elfogadott spec a lemezen + SPEC_READY checkpoint."""
        coder = self.build([])
        p = os.path.join(self.d, "specs", "T-1.json"); os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f: json.dump(spec or good(), f, ensure_ascii=False)
        self.sup.accept_spec("T-1", {"status": "SPEC_READY", "path": p, "ui": False, "open_questions": []})
        return coder

    def go(self, script, files=("src/coupon.py",), **kw):
        coder = self.build(script)
        p = os.path.join(self.d, "specs", "T-1.json"); os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f: json.dump(kw.pop("spec", None) or good(), f, ensure_ascii=False)
        self.sup.accept_spec("T-1", {"status": "SPEC_READY", "path": p, "ui": False, "open_questions": []})
        return self.sup.code_task("T-1", coder, files=files, **kw)

    def go_prepare(self):
        p = os.path.join(self.d, "specs", "T-1.json"); os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f: json.dump(good(), f, ensure_ascii=False)
        self.sup.accept_spec("T-1", {"status": "SPEC_READY", "path": p, "ui": False, "open_questions": []})

    def status(self): return self.state.get_task("T-1")["status"]


class TestCodeTask(HandoffBase):
    def test_ready_spec_goes_to_coder_and_patch_lands(self):
        self.assertEqual(self.go([GOOD_PATCH()]), "APPLIED")
        self.assertEqual(self.r("src/coupon.py"), IMPL); self.assertTrue(self.exists("tests/unit/test_coupon.py"))
        self.assertEqual(self.status(), "PATCH_APPLIED"); self.assertEqual(self.state.last_checkpoint("T-1")["step"], "PATCH_APPLIED")
        self.assertIn("PATCH_ACCEPTED", self.events())

    def test_applied_is_not_done(self):
        self.go([GOOD_PATCH()])
        self.assertNotIn(self.status(), ("DONE", "CODE_SUBMITTED"))

    def test_without_accepted_spec_no_call_no_tokens(self):
        coder = self.build([])
        self.assertEqual(self.sup.code_task("T-1", coder, files=("src/coupon.py",)), "NOT_READY")
        self.assertEqual(self.gw.stats["calls"], 0); self.assertIn("CODER_NOT_READY", self.events())

    def test_open_questions_spec_never_reaches_coder(self):
        s = good(); s["open_questions"] = ["Halmozható?"]
        coder = self.build([])
        self.assertEqual(self.sup.accept_spec("T-1", {"status": "SPEC_READY", "path": "x", "ui": False, "open_questions": s["open_questions"]}), "NEEDS_ANSWERS")
        self.assertEqual(self.sup.code_task("T-1", coder), "NOT_READY"); self.assertEqual(self.gw.stats["calls"], 0)

    def test_halted_supervisor_does_not_call_coder(self):
        coder = self.ready(); self.sup.halted = True
        self.assertEqual(self.sup.code_task("T-1", coder, files=("src/coupon.py",)), "HALT"); self.assertEqual(self.gw.stats["calls"], 0)

    def test_escalate_human_opens_gate_and_writes_nothing(self):
        r = self.go([out([edit("src/coupon.py", ORIG, IMPL)], status="ESCALATE_HUMAN", reason="törlés kell", kind="other")])
        self.assertEqual(r, "ESCALATED"); self.assertEqual(self.r("src/coupon.py"), ORIG)
        self.assertEqual(len(self.state.pending_gates()), 1); self.assertEqual(self.status(), "WAITING_HUMAN")

    def test_protected_zone_rejected_goes_to_human_without_retry(self):
        r = self.go([out([], [newf("tests/acceptance/test_x.py", "x = 1\n")])])
        self.assertEqual(r, "ESCALATED"); self.assertFalse(self.exists("tests/acceptance/test_x.py"))
        self.assertEqual(self.gw.stats["calls"], 1); self.assertEqual(len(self.state.pending_gates()), 1)

    def test_search_error_retried_once_then_human(self):
        bad = out([edit("src/coupon.py", "nincs ilyen sor\n", IMPL)])
        self.assertEqual(self.go([bad, bad]), "ESCALATED"); self.assertEqual(self.gw.stats["calls"], 2)
        self.assertEqual(self.r("src/coupon.py"), ORIG)

    def test_search_error_repaired_on_retry_is_applied(self):
        bad = out([edit("src/coupon.py", "nincs ilyen sor\n", IMPL)])
        self.assertEqual(self.go([bad, GOOD_PATCH()]), "APPLIED")

    def test_spec_invalid_goes_to_human_zero_tokens(self):
        s = good(); s["acceptance_criteria"] = s["acceptance_criteria"][:1]; s["scope_out"] = []
        self.assertEqual(self.go([], spec=s), "ESCALATED"); self.assertEqual(self.gw.stats["calls"], 0)

    def test_context_too_large_goes_to_human_zero_tokens(self):
        self.w("src/big.py", "x = 1\n" * 4000)
        self.assertEqual(self.go([], files=("src/big.py",)), "ESCALATED"); self.assertEqual(self.gw.stats["calls"], 0)
        self.assertIn("nem fér a keretbe", json.dumps(self.state.pending_gates(), default=str, ensure_ascii=False))

    def test_bad_context_path_goes_to_human(self):
        self.assertEqual(self.go([], files=(".env",)), "ESCALATED"); self.assertEqual(self.gw.stats["calls"], 0)

    def test_output_failure_halts_thread(self):
        self.assertEqual(self.go([{"fault": "schema_violation"}, {"fault": "schema_violation"}]), "HALT")
        self.assertEqual(self.status(), "HALTED"); self.assertEqual(self.r("src/coupon.py"), ORIG)

    def test_token_cap_halts_and_applies_nothing(self):
        self.cfg["iteration_policy"]["caps"]["S2"]["tokens"] = 1
        self.assertEqual(self.go([GOOD_PATCH()]), "HALT"); self.assertEqual(self.r("src/coupon.py"), ORIG)

    def test_daily_quota_pauses_with_checkpoint(self):
        coder = self.ready(); coder.run = lambda *a, **k: (_ for _ in ()).throw(DailyQuotaExhausted("napi"))
        self.assertEqual(self.sup.code_task("T-1", coder, files=("src/coupon.py",)), "PAUSED")
        self.assertEqual(self.status(), "PAUSED_DAILY_LIMIT"); self.assertEqual(self.state.last_checkpoint("T-1")["step"], "PAUSED_DAILY_LIMIT")

    def test_wait_too_long_goes_to_human(self):
        coder = self.ready(); coder.run = lambda *a, **k: (_ for _ in ()).throw(WaitTooLong(500))
        self.assertEqual(self.sup.code_task("T-1", coder, files=("src/coupon.py",)), "ESCALATED"); self.assertEqual(len(self.state.pending_gates()), 1)

    def test_kill_switch_propagates(self):
        from factory.killswitch import Killed
        coder = self.ready(); self.kill.trip("teszt")
        with self.assertRaises(Killed): self.sup.code_task("T-1", coder, files=("src/coupon.py",))

    def test_feedback_is_passed_to_coder(self):
        self.go([GOOD_PATCH()], feedback="lint: E501 sor túl hosszú")
        self.assertIn("E501", self.prompt_text())


    def test_second_call_after_applied_is_not_ready(self):
        coder = self.build([GOOD_PATCH(), GOOD_PATCH()]); self.go_prepare()
        self.assertEqual(self.sup.code_task("T-1", coder, files=("src/coupon.py",)), "APPLIED")
        self.assertEqual(self.sup.code_task("T-1", coder, files=("src/coupon.py",)), "NOT_READY"); self.assertEqual(self.gw.stats["calls"], 1)

    def test_no_run_while_waiting_for_human(self):
        coder = self.ready(); self.state.upsert_task("T-1", "WAITING_HUMAN")
        self.assertEqual(self.sup.code_task("T-1", coder, files=("src/coupon.py",)), "NOT_READY"); self.assertEqual(self.gw.stats["calls"], 0)

    def test_resume_after_daily_pause_reaches_coder(self):
        coder = self.build([GOOD_PATCH()]); self.go_prepare(); real = coder.run
        coder.run = lambda *a, **k: (_ for _ in ()).throw(DailyQuotaExhausted("napi"))
        self.assertEqual(self.sup.code_task("T-1", coder, files=("src/coupon.py",)), "PAUSED")
        coder.run = real
        self.assertEqual(self.sup.code_task("T-1", coder, files=("src/coupon.py",)), "APPLIED"); self.assertEqual(self.r("src/coupon.py"), IMPL)

    def test_other_checkpoint_does_not_start_coder(self):
        coder = self.ready(); self.state.checkpoint("T-1", "SPLIT_PROGRESS", {"x": 1})
        self.assertEqual(self.sup.code_task("T-1", coder, files=("src/coupon.py",)), "NOT_READY"); self.assertEqual(self.gw.stats["calls"], 0)

    def test_after_human_decision_coder_can_run_again(self):
        bad = out([edit("src/coupon.py", "nincs ilyen sor\n", IMPL)]); coder = self.build([bad, bad, GOOD_PATCH()]); self.go_prepare()
        self.assertEqual(self.sup.code_task("T-1", coder, files=("src/coupon.py",)), "ESCALATED")
        gid = self.state.pending_gates()[0]; gid = gid["id"] if isinstance(gid, dict) else gid[0]
        self.sup.resolve_gate(gid, "kézi javítás kész", task_id="T-1")
        self.assertEqual(self.sup.code_task("T-1", coder, files=("src/coupon.py",)), "APPLIED")

    def test_spec_ready_checkpoint_without_spec_path_is_not_ready(self):
        coder = self.build([]); self.state.checkpoint("T-1", "SPEC_READY", {})
        self.assertEqual(self.sup.code_task("T-1", coder, files=("src/coupon.py",)), "NOT_READY"); self.assertEqual(self.gw.stats["calls"], 0)


class TestAcceptPatchUnit(HandoffBase):
    def test_unknown_status_raises(self):
        self.build([])
        with self.assertRaises(ValueError): self.sup.accept_patch("T-1", {"status": "???"})

    def test_rejected_does_not_count_as_iteration(self):
        self.build([]); self.sup.accept_patch("T-1", {"status": "PATCH_REJECTED", "errors": ["DENIED x"], "retried": False})
        self.assertEqual(self.state.get_task("T-1")["iteration"], 0)

if __name__ == "__main__": unittest.main()
