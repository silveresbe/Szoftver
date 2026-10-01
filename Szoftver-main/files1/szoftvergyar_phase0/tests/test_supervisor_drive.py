"""Supervisor.drive() gate loop: patch apply -> gates run -> feedback/retry (mock mode, 0 token)."""
import copy, json, os, tempfile, unittest
from factory import config as C
from factory.audit import AuditLog
from factory.state import State
from factory.rate_limiter import RateLimiter
from factory.redactor import Redactor
from factory.gateway import Gateway
from factory.tool_gateway import ToolGateway
from factory.killswitch import KillSwitch
from factory.supervisor import Supervisor
from factory.master_coder import MasterCoder
from tests.test_product_owner import good, bad_dor, Clock, CFG
from tests.test_master_coder import CoderBase, GOOD_PATCH, ORIG, IMPL, TEST, out, edit, newf


class MockGates:
    """Egyszerű mock gates runner az 0. fázis tesztekhez."""
    def __init__(self, results_script=None):
        self.results_script = results_script or []
        self.call_count = 0

    def run(self, task_id):
        result = self.results_script[self.call_count] if self.call_count < len(self.results_script) else {"ok": True}
        self.call_count += 1
        return result


class TestDriveGreen(CoderBase):
    """Supervisor.drive(): PO spec -> Coder -> gates all-green."""

    def go(self, script, gates_script=None, files=("src/coupon.py",)):
        coder = self.build(script)
        p = os.path.join(self.d, "specs", "T-1.json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(good(), f, ensure_ascii=False)
        self.sup.accept_spec("T-1", {"status": "SPEC_READY", "path": p, "ui": False, "open_questions": []})
        gates = MockGates(gates_script or [{"ok": True}])
        return self.sup.drive("T-1", coder, gates, files=files)

    def test_patch_applied_then_gates_green(self):
        r = self.go([GOOD_PATCH()])
        self.assertEqual(r, "GREEN")
        self.assertEqual(self.state.get_task("T-1")["status"], "GATES_GREEN")
        self.assertEqual(self.state.last_checkpoint("T-1")["step"], "GATES_GREEN")
        self.assertEqual(self.r("src/coupon.py"), IMPL)

    def test_gate_failed_retry_then_green(self):
        """Első körben gate fail, második körben patch OK + gate OK."""
        gates = MockGates([
            {"ok": False, "gate": "syntax", "errors": ["syntax hiba"], "fingerprint": "fp1",
             "feedback": "Syntax hiba, javítsd meg."},
            {"ok": True}
        ])
        # Első kör: patch OK de gate fail -> feedback vissza a Codernek
        # Második kör: patch OK + gate OK -> GREEN
        r = self.go([GOOD_PATCH(), GOOD_PATCH()], gates_script=gates.results_script)
        self.assertEqual(r, "GREEN")
        self.assertEqual(self.gw.stats["calls"], 2)  # 2 Coder hívás
        self.assertIn("Syntax hiba", self.prompt_text(1))  # Feedback a második promptban

    def test_circuit_breaker_same_error_3_times(self):
        """Ugyanaz az ujjlenyomat 3× -> Circuit Breaker -> HALT."""
        gates = MockGates([
            {"ok": False, "gate": "test", "errors": ["test fail"], "fingerprint": "fp_same", "feedback": "Test fail."},
            {"ok": False, "gate": "test", "errors": ["test fail"], "fingerprint": "fp_same", "feedback": "Test fail."},
            {"ok": False, "gate": "test", "errors": ["test fail"], "fingerprint": "fp_same", "feedback": "Test fail."},
        ])
        r = self.go([GOOD_PATCH(), GOOD_PATCH(), GOOD_PATCH(), GOOD_PATCH()], gates_script=gates.results_script)
        self.assertEqual(r, "HALT")
        self.assertEqual(self.state.get_task("T-1")["status"], "HALTED")
        self.assertIn("Circuit Breaker", json.dumps(self.state.pending_gates(), default=str))

    def test_iteration_cap_exceeded(self):
        """Iterációs plafon elérése -> escalate to human -> CAP."""
        self.cfg["iteration_policy"]["caps"]["S2"]["iterations"] = 1
        gates = MockGates([
            {"ok": False, "gate": "test", "errors": ["fail 1"], "fingerprint": "fp1", "feedback": "Fail."},
        ])
        r = self.go([GOOD_PATCH(), GOOD_PATCH()], gates_script=gates.results_script)
        self.assertEqual(r, "CAP")
        self.assertEqual(self.state.get_task("T-1")["status"], "HALTED")
        self.assertEqual(len(self.state.pending_gates()), 1)

    def test_no_progress_error_not_shrinking(self):
        """3 körben az errors nem csökken -> NO_PROGRESS (tier escalation)."""
        gates = MockGates([
            {"ok": False, "gate": "test", "errors": ["e1", "e2", "e3"], "fingerprint": "fp1", "feedback": "Fail."},
            {"ok": False, "gate": "test", "errors": ["e1", "e2", "e3"], "fingerprint": "fp2", "feedback": "Fail."},
            {"ok": False, "gate": "test", "errors": ["e1", "e2", "e3"], "fingerprint": "fp3", "feedback": "Fail."},
        ])
        r = self.go(
            [GOOD_PATCH(), GOOD_PATCH(), GOOD_PATCH(), GOOD_PATCH()],
            gates_script=gates.results_script
        )
        self.assertEqual(r, "NO_PROGRESS")
        d = self.state.get_task_data("T-1")
        self.assertTrue(d.get("tier_escalated"))

    def test_gate_failed_then_continue_next_iteration(self):
        """Gate fail -> continue (nem NO_PROGRESS még) -> next loop."""
        gates = MockGates([
            {"ok": False, "gate": "test", "errors": ["a", "b"], "fingerprint": "fp1", "feedback": "Fail 1."},
            {"ok": False, "gate": "test", "errors": ["a"], "fingerprint": "fp2", "feedback": "Fail 2."},
            {"ok": True},
        ])
        r = self.go(
            [GOOD_PATCH(), GOOD_PATCH(), GOOD_PATCH()],
            gates_script=gates.results_script
        )
        self.assertEqual(r, "GREEN")
        self.assertEqual(self.gw.stats["calls"], 3)


class TestDrivePaused(CoderBase):
    """Supervisor.drive(): DailyQuotaExhausted, resume logic."""

    def test_coder_throws_daily_quota_exhausted(self):
        """code_task() dobja a DailyQuotaExhausted -> PAUSED."""
        coder = self.build([])
        p = os.path.join(self.d, "specs", "T-1.json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(good(), f, ensure_ascii=False)
        self.sup.accept_spec("T-1", {"status": "SPEC_READY", "path": p, "ui": False, "open_questions": []})
        from factory.rate_limiter import DailyQuotaExhausted
        coder.run = lambda *a, **k: (_ for _ in ()).throw(DailyQuotaExhausted("napi"))
        gates = MockGates([{"ok": True}])
        r = self.sup.drive("T-1", coder, gates, files=("src/coupon.py",))
        self.assertEqual(r, "PAUSED")
        self.assertEqual(self.state.get_task("T-1")["status"], "PAUSED_DAILY_LIMIT")
        cp = self.state.last_checkpoint("T-1")
        self.assertEqual(cp["step"], "PAUSED_DAILY_LIMIT")
        self.assertEqual(cp["data"].get("resume"), "code_task")


class TestDriveEscalate(CoderBase):
    """Supervisor.drive(): SandboxRefused, GateToolsMissing -> escalate."""

    def test_sandbox_refused_escalates(self):
        """gates.run() dobja SandboxRefused -> ESCALATED."""
        coder = self.build([GOOD_PATCH()])
        p = os.path.join(self.d, "specs", "T-1.json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(good(), f, ensure_ascii=False)
        self.sup.accept_spec("T-1", {"status": "SPEC_READY", "path": p, "ui": False, "open_questions": []})
        from factory.sandbox import SandboxRefused

        class FailingGates:
            def run(self, task_id):
                raise SandboxRefused("sandbox offline")

        gates = FailingGates()
        r = self.sup.drive("T-1", coder, gates, files=("src/coupon.py",))
        self.assertEqual(r, "ESCALATED")
        self.assertEqual(self.state.get_task("T-1")["status"], "WAITING_HUMAN")
        self.assertEqual(len(self.state.pending_gates()), 1)


if __name__ == "__main__":
    unittest.main()
