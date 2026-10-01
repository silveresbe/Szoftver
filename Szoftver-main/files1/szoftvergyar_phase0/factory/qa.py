"""QA review gate: determinisztikus elfogadási logika a patch-ekhez.

A QA modul a patchet a feladat specifikáció és a tesztfutás alapján értékeli. A
cél a döntés egyszerű, reprodukálható formában történő visszaadása:
- ACCEPT: a patch megfelel a kritériumoknak
- REJECT: a patchet el kell utasítani
- NEEDS_INFO: a QA további információt kér
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


class QAError(RuntimeError):
    """A QA modulhoz tartozó determinisztikus hiba."""


class QA:
    """A patch értékelő kapu. A QA a különböző köztes eredményeket egyetlen
    strukturált döntéssé birodalítja.
    """

    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    NEEDS_INFO = "NEEDS_INFO"

    def __init__(self, audit=None):
        self.audit = audit

    def run(
        self,
        task_id: str,
        spec: dict[str, Any],
        patch_files: dict[str, Any] | Sequence[str] | None,
        test_results: dict[str, Any] | None = None,
        iteration: int = 0,
        feedback: str | None = None,
    ) -> dict[str, Any]:
        """Futtassa le a QA értékelést a feladathoz.

        Visszatérési forma:
        {
            "task_id": ...,
            "status": "ACCEPT|REJECT|NEEDS_INFO",
            "iteration": ...,
            "score": 0.0-1.0,
            "summary": "...",
            "reason": "...",
            "missing_files": [...],
            "failing_tests": [...],
            "passed_tests": [...],
            "evidence": {...},
            "feedback": "..."
        }
        """
        if not isinstance(spec, Mapping):
            raise QAError("spec must be a mapping / dict")

        if not task_id:
            raise QAError("task_id cannot be empty")

        files = self._normalize_patch_files(patch_files)
        tests = self._normalize_test_results(test_results)
        required_files = self._as_list(spec.get("required_files") or spec.get("files") or spec.get("must_modify"))
        required_tests = self._as_list(spec.get("required_tests") or spec.get("tests"))
        acceptance = self._as_list(spec.get("acceptance_criteria"))

        missing_files = self._missing_required_files(files, required_files)
        missing_tests = self._missing_required_tests(tests, required_tests)
        failing_tests = self._failing_tests(tests)
        passed_tests = self._passed_tests(tests)

        if missing_files:
            return self._decision(
                task_id,
                status=self.REJECT,
                iteration=iteration,
                score=0.0,
                summary="A patch hiányzó fájlokat nem tartalmaz.",
                reason=f"Hiányzó fájlok: {', '.join(missing_files)}",
                missing_files=missing_files,
                failing_tests=failing_tests,
                passed_tests=passed_tests,
                evidence={"required_files": required_files, "patch_files": files},
                feedback=feedback,
            )

        if failing_tests:
            return self._decision(
                task_id,
                status=self.REJECT,
                iteration=iteration,
                score=max(0.0, 1.0 - (len(failing_tests) / max(1, len(failing_tests) + len(passed_tests))),
                ),
                summary="A patch nem felel meg a tesztkritériumoknak.",
                reason=f"Hibás tesztek: {', '.join(failing_tests)}",
                missing_files=missing_files,
                failing_tests=failing_tests,
                passed_tests=passed_tests,
                evidence={"test_results": tests, "acceptance_criteria": acceptance},
                feedback=feedback,
            )

        if missing_tests:
            return self._decision(
                task_id,
                status=self.NEEDS_INFO,
                iteration=iteration,
                score=0.5,
                summary="A patch még hiányzik a kötelező ellenőrzésekhez.",
                reason=f"Hiányzó kötelező tesztek: {', '.join(missing_tests)}",
                missing_files=missing_files,
                failing_tests=failing_tests,
                passed_tests=passed_tests,
                evidence={"required_tests": required_tests, "test_results": tests},
                feedback=feedback,
            )

        if acceptance and not self._acceptance_met(acceptance, files, tests):
            return self._decision(
                task_id,
                status=self.NEEDS_INFO,
                iteration=iteration,
                score=0.6,
                summary="A patch nem teljesíti az explicit acceptancia kritériumokat.",
                reason="Néhány acceptancia feltétel nem ellenőrizhető vagy nem teljesült.",
                missing_files=missing_files,
                failing_tests=failing_tests,
                passed_tests=passed_tests,
                evidence={"acceptance_criteria": acceptance, "patch_files": files, "test_results": tests},
                feedback=feedback,
            )

        return self._decision(
            task_id,
            status=self.ACCEPT,
            iteration=iteration,
            score=1.0,
            summary="A patch megfelel a specifikációnak és a QA feltételeknek.",
            reason="Nincs hiányzó fájl, nincs sikertelen teszt és az acceptancia kritériumok teljesülnek.",
            missing_files=missing_files,
            failing_tests=failing_tests,
            passed_tests=passed_tests,
            evidence={"patch_files": files, "test_results": tests},
            feedback=feedback,
        )

    @staticmethod
    def _normalize_patch_files(patch_files: dict[str, Any] | Sequence[str] | None) -> list[str]:
        if patch_files is None:
            return []
        if isinstance(patch_files, Mapping):
            return [str(k) for k in patch_files.keys()]
        if isinstance(patch_files, (list, tuple, set)):
            return [str(x) for x in patch_files]
        return [str(patch_files)]

    @staticmethod
    def _normalize_test_results(test_results: dict[str, Any] | None) -> dict[str, Any]:
        if test_results is None:
            return {}
        if isinstance(test_results, Mapping):
            return dict(test_results)
        return {"raw": test_results}

    @staticmethod
    def _as_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple, set)):
            return [str(x) for x in value]
        return [str(value)]

    @staticmethod
    def _missing_required_files(patch_files: list[str], required_files: list[str]) -> list[str]:
        if not required_files:
            return []
        return [f for f in required_files if not any(f == p or p.endswith(f) for p in patch_files)]

    @staticmethod
    def _missing_required_tests(test_results: Mapping[str, Any], required_tests: list[str]) -> list[str]:
        if not required_tests:
            return []
        names = []
        for key in test_results:
            if isinstance(test_results[key], Mapping):
                names.append(str(test_results[key].get("name") or key))
            else:
                names.append(str(key))
        return [name for name in required_tests if name not in names]

    @staticmethod
    def _failing_tests(test_results: Mapping[str, Any]) -> list[str]:
        names: list[str] = []
        for key, value in test_results.items():
            if isinstance(value, Mapping):
                outcome = value.get("status") or value.get("result") or value.get("passed")
                name = str(value.get("name") or key)
            else:
                outcome = value
                name = str(key)

            if isinstance(outcome, bool):
                if not outcome:
                    names.append(name)
            elif isinstance(outcome, str):
                lowered = outcome.lower()
                if lowered in {"failed", "error", "fail", "failure", "broken", "timeout"}:
                    names.append(name)
        return names

    @staticmethod
    def _passed_tests(test_results: Mapping[str, Any]) -> list[str]:
        names: list[str] = []
        for key, value in test_results.items():
            if isinstance(value, Mapping):
                outcome = value.get("status") or value.get("result") or value.get("passed")
                name = str(value.get("name") or key)
            else:
                outcome = value
                name = str(key)

            if isinstance(outcome, bool):
                if outcome:
                    names.append(name)
            elif isinstance(outcome, str):
                lowered = outcome.lower()
                if lowered in {"pass", "passed", "ok", "success", "successfully"}:
                    names.append(name)
        return names

    @staticmethod
    def _acceptance_met(acceptance: list[str], files: list[str], tests: Mapping[str, Any]) -> bool:
        if not acceptance:
            return True
        if not files and not tests:
            return False
        for criterion in acceptance:
            text = str(criterion).lower()
            if "file" in text and not files:
                return False
            if "test" in text and not tests:
                return False
        return True

    @staticmethod
    def _decision(
        task_id: str,
        *,
        status: str,
        iteration: int,
        score: float,
        summary: str,
        reason: str,
        missing_files: list[str],
        failing_tests: list[str],
        passed_tests: list[str],
        evidence: dict[str, Any],
        feedback: str | None,
    ) -> dict[str, Any]:
        return {
            "task_id": task_id,
            "status": status,
            "iteration": iteration,
            "score": round(max(0.0, min(1.0, float(score))), 3),
            "summary": summary,
            "reason": reason,
            "missing_files": missing_files,
            "failing_tests": failing_tests,
            "passed_tests": passed_tests,
            "evidence": evidence,
            "feedback": feedback,
        }


DEFAULT_QA = QA


if __name__ == "__main__":
    qa = QA()
    print(
        qa.run(
            "TASK-001",
            {"required_files": ["factory/qa.py"], "acceptance_criteria": ["patch file", "tests pass"]},
            {"factory/qa.py": "ok"},
            {"pytest": True},
            iteration=1,
        )
    )
