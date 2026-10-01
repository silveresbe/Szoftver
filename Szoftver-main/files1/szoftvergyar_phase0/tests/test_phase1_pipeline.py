"""Smoke tests for the phase-1 pipeline: lane inference, QA approval, and Git commit formatting."""
from __future__ import annotations

import os
import tempfile
import unittest

from factory.audit import AuditLog
from factory.git_agent import GitDevOps
from factory.lane_classifier import LaneClassifier
from factory.qa import QA
from factory.state import State
from factory.supervisor import Supervisor


class FakeGateway:
    def call(self, *args, **kwargs):
        return {"data": {"verdict": "APPROVE", "additional_issues": []}, "tokens": 12}


class TestPhase1Smoke(unittest.TestCase):
    def test_lane_classifier_s1_and_s2(self):
        c = LaneClassifier()
        self.assertEqual(c.classify("Fix typo in README"), "S1")
        self.assertEqual(c.classify("Add OAuth secret token flow with database migration"), "S2")

    def test_supervisor_infers_lane_from_task_text(self):
        cfg = {
            "rollout": {"phase": 1},
            "iteration_policy": {"caps": {"S1": {"iterations": 3, "tokens": 2000}, "S2": {"iterations": 5, "tokens": 5000}}},
            "circuit_breaker": {"same_fingerprint_iterations": 3},
        }
        d = tempfile.mkdtemp()
        state = State(os.path.join(d, "state.db"))
        audit = AuditLog(os.path.join(d, "audit.jsonl"))
        sup = Supervisor(cfg, state, audit)
        sup.add_task("T-1", data={"task": "Fix typo in README"})
        self.assertEqual(state.get_task("T-1")["lane"], "S1")
        state.close(); audit.close()

    def test_qa_approves_clean_patch(self):
        qa = QA({}, gateway=FakeGateway(), audit=None, state=None, supervisor=None)
        spec = {
            "acceptance_criteria": [
                {"id": "AC-1", "kind": "happy", "given": "user enters valid data", "when": "they submit", "then": "it succeeds"}
            ],
            "stories": [{"id": "S-1", "ui": False}],
            "scope_out": [],
            "open_questions": [],
        }
        patch = {"src/app.py": "def run():\n    return 42\n"}
        result = qa.run("T-1", spec, patch, test_results={"mod.py": {"passed": 3, "failed": 0, "coverage": 0.9}})
        self.assertEqual(result["verdict"], "APPROVE")

    def test_git_commit_message_format(self):
        git = GitDevOps({}, audit=None, sandbox=None)
        msg = git._format_commit_message("T-1", {"files": ["src/app.py"], "summary": "Fix typo"})
        self.assertIn("feat(src)", msg)
        self.assertIn("Fix typo", msg)


if __name__ == "__main__":
    unittest.main()
