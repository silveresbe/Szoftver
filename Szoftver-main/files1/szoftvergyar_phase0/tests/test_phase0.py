import copy
import os
import tempfile
import unittest
import yaml

from factory import config as C
from factory.handoff_flow import HandoffFlow
from factory.lane_classifier import LaneClassifier
from factory.qa import QA
from factory.supervisor import Supervisor
from factory.task_runtime import TaskRuntime
from factory.task_state import TASK_STATES
from factory.workflow_orchestrator import WorkflowOrchestrator

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, "factory.yaml")) as _f:
    CFG = yaml.safe_load(_f)


class TestHandoffFlow(unittest.TestCase):
    def test_review_patch_routes_accept_to_owner(self):
        flow = HandoffFlow()
        result = flow.review_patch(
            "T-100",
            {"required_files": ["factory/qa.py"], "acceptance_criteria": ["patch file", "tests pass"]},
            {"factory/qa.py": "ok"},
            {"unit": True},
            iteration=2,
        )
        self.assertEqual(result["state"], "ACCEPTED")
        self.assertEqual(result["next_action"], "handoff_to_owner")
        self.assertEqual(result["recipient"], "product_owner")

    def test_review_patch_rejects_failing_tests(self):
        flow = HandoffFlow()
        result = flow.review_patch(
            "T-101",
            {"required_files": ["factory/qa.py"], "acceptance_criteria": ["patch file", "tests pass"]},
            {"factory/qa.py": "ok"},
            {"unit": False},
            iteration=1,
        )
        self.assertEqual(result["state"], "REJECTED")
        self.assertEqual(result["recipient"], "master_coder")

    def test_accept_or_escalate_needs_info_uses_human_gate(self):
        flow = HandoffFlow()
        result = flow.accept_or_escalate(
            "T-102",
            {"status": "NEEDS_INFO", "score": 0.6, "summary": "Missing evidence", "reason": "Requires extra proof"},
            iteration=1,
        )
        self.assertEqual(result["state"], "HUMAN_ESCALATION")
        self.assertEqual(result["recipient"], "human")


class TestRuntimeWorkflow(unittest.TestCase):
    def test_task_runtime_accept_path(self):
        result = TaskRuntime.run_workflow(
            "T-200",
            {"required_files": ["factory/qa.py"], "acceptance_criteria": ["patch file", "tests pass"]},
            {"factory/qa.py": "ok"},
            {"unit": True},
            current_state="IN_PROGRESS",
            iteration=1,
        )
        self.assertEqual(result.next_state, "ACCEPTED")
        self.assertEqual(result.next_action, "handoff_to_owner")
        self.assertEqual(result.recipient, "product_owner")

    def test_task_runtime_reject_path(self):
        result = TaskRuntime.run_workflow(
            "T-201",
            {"required_files": ["factory/qa.py"], "acceptance_criteria": ["patch file", "tests pass"]},
            {"factory/qa.py": "ok"},
            {"unit": False},
            current_state="IN_PROGRESS",
            iteration=1,
        )
        self.assertEqual(result.next_state, "REJECTED")
        self.assertEqual(result.recipient, "master_coder")


class TestWorkflowOrchestrator(unittest.TestCase):
    def test_orchestrator_complete_accept_flow(self):
        orch = WorkflowOrchestrator()
        result = orch.run(
            "T-300",
            {
                "required_files": ["factory/qa.py"],
                "acceptance_criteria": ["patch file", "tests pass"],
                "stories": [{"id": "S-1", "title": "test"}],
                "scope_in": ["QA module"],
                "scope_out": ["deployment"],
                "assumptions": [{"text": "python 3.10+", "needs_confirmation": True}],
            },
            {"factory/qa.py": "ok"},
            {"unit": True},
            current_state=TASK_STATES["IN_PROGRESS"],
            iteration=1,
        )
        self.assertEqual(result.next_state, TASK_STATES["ACCEPTED"])
        self.assertEqual(result.next_action, "handoff_to_owner")
        self.assertEqual(result.recipient, "product_owner")
        self.assertEqual(result.route, "accepted")

    def test_orchestrator_reject_flow(self):
        orch = WorkflowOrchestrator()
        result = orch.run(
            "T-301",
            {
                "required_files": ["factory/qa.py"],
                "acceptance_criteria": ["patch file", "tests pass"],
                "stories": [{"id": "S-1", "title": "test"}],
                "scope_in": ["QA module"],
                "scope_out": ["deployment"],
                "assumptions": [{"text": "python 3.10+", "needs_confirmation": True}],
            },
            {"factory/qa.py": "ok"},
            {"unit": False},
            current_state=TASK_STATES["IN_PROGRESS"],
            iteration=1,
        )
        self.assertEqual(result.next_state, TASK_STATES["REJECTED"])
        self.assertEqual(result.recipient, "master_coder")
        self.assertEqual(result.route, "revision_required")

    def test_orchestrator_needs_info_escalation(self):
        orch = WorkflowOrchestrator()
        result = orch.run(
            "T-302",
            {
                "required_files": ["factory/qa.py"],
                "acceptance_criteria": ["patch file", "tests pass"],
                "stories": [{"id": "S-1", "title": "test"}],
                "scope_in": ["QA module"],
                "scope_out": ["deployment"],
                "assumptions": [{"text": "python 3.10+", "needs_confirmation": True}],
            },
            {"factory/qa.py": "ok"},
            {},  # no test results
            current_state=TASK_STATES["IN_PROGRESS"],
            iteration=1,
        )
        self.assertEqual(result.next_state, TASK_STATES["HUMAN_ESCALATION"])
        self.assertEqual(result.recipient, "human")

    def test_orchestrator_human_continue_resolution(self):
        orch = WorkflowOrchestrator()
        result = orch.run(
            "T-303",
            {
                "required_files": ["factory/qa.py"],
                "acceptance_criteria": ["patch file", "tests pass"],
                "stories": [{"id": "S-1", "title": "test"}],
                "scope_in": ["QA module"],
                "scope_out": ["deployment"],
                "assumptions": [{"text": "python 3.10+", "needs_confirmation": True}],
            },
            {"factory/qa.py": "ok"},
            {},
            current_state=TASK_STATES["IN_PROGRESS"],
            iteration=1,
            human_decision="CONTINUE",
        )
        self.assertEqual(result.next_state, TASK_STATES["PENDING"])
        self.assertIn("human_escalation_resolved", result.route)

    def test_orchestrator_human_halt_resolution(self):
        orch = WorkflowOrchestrator()
        result = orch.run(
            "T-304",
            {
                "required_files": ["factory/qa.py"],
                "acceptance_criteria": ["patch file", "tests pass"],
                "stories": [{"id": "S-1", "title": "test"}],
                "scope_in": ["QA module"],
                "scope_out": ["deployment"],
                "assumptions": [{"text": "python 3.10+", "needs_confirmation": True}],
            },
            {"factory/qa.py": "ok"},
            {},
            current_state=TASK_STATES["IN_PROGRESS"],
            iteration=1,
            human_decision="HALT",
        )
        self.assertEqual(result.next_state, TASK_STATES["HALTED"])


class TestLaneClassifier(unittest.TestCase):
    def test_easy_doc_fix_is_s1(self):
        cl = LaneClassifier()
        self.assertEqual(cl.classify("Fix typo in README and update docs"), "S1")
        self.assertEqual(cl.classify({"text": "Fix typo in README", "size": "small", "kind": "docs"}), "S1")

    def test_security_and_refactor_are_s2(self):
        cl = LaneClassifier()
        self.assertEqual(cl.classify("Add OAuth login flow with auth checks"), "S2")
        self.assertEqual(cl.classify({"text": "Refactor auth module", "files": ["auth.py", "token.py", "session.py"]}), "S2")


class TestQAFlow(unittest.TestCase):
    def test_q_a_normalizes_and_routes(self):
        qa = QA()
        result = qa.run("T-9", {"required_files": ["factory/qa.py"], "acceptance_criteria": ["patch file"]}, {"factory/qa.py": "ok"}, {"unit": True}, iteration=1)
        self.assertEqual(result["status"], "ACCEPT")
        self.assertEqual(QA.normalize_status("accept"), "ACCEPT")

    def test_supervisor_handles_qa_result(self):
        s = Supervisor({"plan", "build", "qa", "handoff"})
        routed = s.handle_qa_result("T-9", {"status": "ACCEPT", "score": 1.0, "summary": "OK", "reason": "works"}, iteration=3)
        self.assertEqual(routed["state"], "ACCEPTED")
        self.assertEqual(routed["next_action"], "handoff_to_owner")
        self.assertEqual(routed["recipient"], "product_owner")


class TestConfig(unittest.TestCase):
    def test_sample_valid(self):
        C.validate(copy.deepcopy(CFG))

    def test_network_must_be_none(self):
        c = copy.deepcopy(CFG)
        c["sandbox"]["network"] = "bridge"
        with self.assertRaises(C.ConfigError):
            C.validate(c)


if __name__ == "__main__":
    unittest.main()
