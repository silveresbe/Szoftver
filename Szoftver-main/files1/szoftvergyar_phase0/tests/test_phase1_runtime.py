"""Lifecycle and runtime smoke tests for phase 1 stabilization.

These tests validate the deterministic task lifecycle and ensure the runtime API
returns stable, valid task states and payloads.
"""
from __future__ import annotations

import unittest

from task_lifecycle import TaskLifecycle, LifecycleManager, VALID_STATES
from phase1_runtime_api import RuntimeAPI


class TestLifecycle(unittest.TestCase):
    def test_valid_states(self):
        self.assertTrue(all(state in VALID_STATES for state in ["PENDING", "IN_PROGRESS", "QA_REVIEW", "ACCEPTED", "REJECTED", "NEEDS_INFO"]))

    def test_valid_transition(self):
        self.assertEqual(TaskLifecycle.transition("IN_PROGRESS", "QA_REVIEW"), "QA_REVIEW")

    def test_illegal_transition_raises(self):
        with self.assertRaises(ValueError):
            TaskLifecycle.transition("ACCEPTED", "IN_PROGRESS")

    def test_human_decision_normalization(self):
        self.assertEqual(TaskLifecycle.normalize_human_decision("continue"), "RESUMED")
        self.assertEqual(TaskLifecycle.normalize_human_decision("halt"), "HALTED")
        self.assertEqual(TaskLifecycle.normalize_human_decision("discard"), "DISCARDED")

    def test_lifecycle_manager_records_event(self):
        manager = LifecycleManager()
        event = manager.apply_transition("T-1", "PENDING", "IN_PROGRESS", source="test")
        self.assertEqual(event.from_state, "PENDING")
        self.assertEqual(event.to_state, "IN_PROGRESS")
        self.assertEqual(len(manager.events), 1)


class TestRuntimeAPI(unittest.TestCase):
    def test_create_task_and_run(self):
        api = RuntimeAPI()
        api.create_task(
            "TASK-API-1",
            spec={"required_files": ["factory/qa.py"], "acceptance_criteria": ["patch file", "tests pass"]},
            patch_files={"factory/qa.py": "ok"},
            test_results={"unit": True},
        )
        result = api.run_task("TASK-API-1", iteration=1)
        self.assertIn(result["next_state"], {"ACCEPTED", "REJECTED", "HUMAN_ESCALATION", "NEEDS_INFO"})
        self.assertIn("task_id", result)
        self.assertIn("next_action", result)

    def test_resume_human_decision(self):
        api = RuntimeAPI()
        api.create_task(
            "TASK-API-2",
            spec={"required_files": ["factory/qa.py"], "acceptance_criteria": ["patch file"]},
            patch_files={"factory/qa.py": "ok"},
            test_results={},
        )
        result = api.run_task("TASK-API-2", iteration=1)
        if result["next_state"] == "HUMAN_ESCALATION":
            resumed = api.resume_task("TASK-API-2", "continue")
            self.assertIn(resumed["next_state"], {"RESUMED", "IN_PROGRESS", "QA_REVIEW", "HALTED", "DISCARDED"})


if __name__ == "__main__":
    unittest.main()
