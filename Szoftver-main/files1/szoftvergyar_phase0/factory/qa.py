"""QA & Reviewer ágens (2.4) – determinisztikus + mock LLM rész."""
from __future__ import annotations
import json, re
from . import messages as M

class QAError(Exception):
    pass

class QA:
    """QA & Reviewer ágens: spec vs patch ellenőrzés, test coverage, output contract."""
    
    def __init__(self, cfg, gateway=None, audit=None, state=None, supervisor=None):
        self.cfg = cfg
        self.gateway = gateway
        self.audit = audit
        self.state = state
        self.supervisor = supervisor
    
    def run(self, task_id: str, spec: dict, patch_files: dict, test_results: dict | None = None, 
            iteration: int = 0, feedback: str | None = None) -> dict:
        """
        QA futtatás: spec + patch ellenőrzés.
        patch_files: {fájl: tartalom}
        test_results: {file: {passed: N, failed: N, errors: []}}
        Visszatér: {status: 'APPROVE'|'REJECT', findings: [], confidence: 0..1}
        """
        findings = []
        confidence = 1.0
        
        # 1. Spec vs AC ellenőrzés
        ac_coverage = self._check_ac_coverage(spec, patch_files)
        if ac_coverage["uncovered"]:
            findings.append({
                "severity": "high",
                "type": "ac_uncovered",
                "message": f"AC-k fedetlen: {', '.join(ac_coverage['uncovered'][:3])}",
                "count": len(ac_coverage["uncovered"])
            })
            confidence -= 0.3
        
        # 2. Teszteredmények
        if test_results:
            test_check = self._check_tests(test_results)
            if test_check["failed"] > 0:
                findings.append({
                    "severity": "high",
                    "type": "test_failure",
                    "message": f"{test_check['failed']} teszt sikertelen",
                    "failed_tests": test_check.get("failures", [])[:5]
                })
                confidence -= 0.4
            elif test_check["coverage"] and test_check["coverage"] < 0.7:
                findings.append({
                    "severity": "medium",
                    "type": "low_coverage",
                    "message": f"Alacsony test coverage: {test_check['coverage']:.0%}",
                    "coverage": test_check["coverage"]
                })
                confidence -= 0.1
        
        # 3. Spec konzisztencia (vague words, scope out megsértés)
        spec_check = self._check_spec_consistency(spec, patch_files)
        if spec_check["violations"]:
            findings.extend(spec_check["violations"])
            confidence -= len(spec_check["violations"]) * 0.05
        
        # 4. Mock LLM review (ha gateway van)
        if self.gateway and iteration < 2:  # csak az első 2 iteráción
            try:
                llm_review = self._gateway_review(task_id, spec, patch_files, findings, feedback)
                if llm_review.get("additional_findings"):
                    findings.extend(llm_review["additional_findings"])
                    confidence = llm_review.get("confidence", confidence)
            except Exception as e:
                if self.audit:
                    self.audit.append("qa", "REVIEW_ERROR", {"task_id": task_id, "error": str(e)})
        
        # 5. Döntés
        confidence = max(0, min(1, confidence))
        verdict = "APPROVE" if confidence >= 0.7 and not findings else "REJECT"
        
        result = {
            "task_id": task_id,
            "status": "QA_DONE",
            "verdict": verdict,
            "findings": findings,
            "confidence": confidence,
            "iteration": iteration,
            "tokens_used": 0
        }
        
        if self.audit:
            self.audit.append("qa", "REVIEW_DONE", {
                "task_id": task_id,
                "verdict": verdict,
                "findings_count": len(findings)
            })
        
        if self.state and self.supervisor:
            self.state.bus_put("supervisor", task_id, M.dumps(M.make(
                task_id, "qa", "supervisor", "REVIEW_RESULT",
                {"findings": findings[:10], "verdict": verdict},
                iteration=iteration
            )))
        
        return result
    
    def _check_ac_coverage(self, spec: dict, patch_files: dict) -> dict:
        """Acceptance Criteria fedettség ellenőrzése."""
        acs = spec.get("acceptance_criteria", [])
        uncovered = []
        
        patch_text = "\n".join(patch_files.values()) if patch_files else ""
        
        for ac in acs:
            ac_id = ac.get("id", "")
            # Nagyon egyszerű heurisztika: a teszt fájlban vagy a kódban van-e AC riferencia
            if ac_id not in patch_text and ac.get("kind") == "happy":
                # Happy path AC-k kötelezőek
                uncovered.append(ac_id)
        
        return {"uncovered": uncovered, "total": len(acs)}
    
    def _check_tests(self, test_results: dict) -> dict:
        """Test eredmények feldolgozása."""
        total_passed = 0
        total_failed = 0
        total_coverage = 0
        coverage_count = 0
        failures = []
        
        for file, result in test_results.items():
            passed = result.get("passed", 0)
            failed = result.get("failed", 0)
            total_passed += passed
            total_failed += failed
            
            if failed > 0:
                failures.append(f"{file}: {failed} hiba")
            
            if "coverage" in result:
                total_coverage += result["coverage"]
                coverage_count += 1
        
        avg_coverage = total_coverage / coverage_count if coverage_count > 0 else None
        
        return {
            "passed": total_passed,
            "failed": total_failed,
            "coverage": avg_coverage,
            "failures": failures
        }
    
    def _check_spec_consistency(self, spec: dict, patch_files: dict) -> dict:
        """Spec vs patch konzisztencia."""
        violations = []
        patch_text = "\n".join(patch_files.values()) if patch_files else ""
        
        # Scope_out ellenőrzés: nem szabad benne lennie a patchben
        scope_out = spec.get("scope_out", [])
        for item in scope_out:
            if item.lower() in patch_text.lower():
                violations.append({
                    "severity": "high",
                    "type": "scope_violation",
                    "message": f"Scope out megsértés: '{item}' a patchben"
                })
        
        # Vague acceptance criteria ellenőrzése
        acs = spec.get("acceptance_criteria", [])
        vague_words = self._find_vague_words(acs)
        if vague_words:
            violations.append({
                "severity": "medium",
                "type": "vague_spec",
                "message": f"Homályos szavak az AC-ben: {', '.join(vague_words[:3])}",
                "words": vague_words
            })
        
        return {"violations": violations}
    
    def _find_vague_words(self, acs: list) -> list:
        """Homályos szavak keresése (valósító nélkül)."""
        vague = {"gyorsan", "jól", "stabilan", "könnyen", "rugalmasan", "hatékonyan", 
                "user-friendly", "intuitive", "robust", "scalable"}
        found = []
        
        for ac in acs:
            for field in ("given", "when", "then"):
                text = ac.get(field, "").lower()
                for word in vague:
                    if word in text and not any(c.isdigit() for c in text):
                        found.append(word)
        
        return list(set(found))
    
    def _gateway_review(self, task_id: str, spec: dict, patch_files: dict, 
                       findings: list, feedback: str | None) -> dict:
        """Mock LLM review a Gatewayen keresztül."""
        if not self.gateway:
            return {}
        
        # Prompt szerkesztés
        prompt_text = f"""Áttekintés szükséges:

Spec (AC-k):
{json.dumps(spec.get('acceptance_criteria', [])[:2], ensure_ascii=False)}

Módosított fájlok ({len(patch_files)} db):
{json.dumps({k: v[:200] + '...' if len(v) > 200 else v for k, v in list(patch_files.items())[:2]}, ensure_ascii=False)}

Eddig talált problémák: {len(findings)}

Döntsd el: APPROVE vagy REJECT? Rövid indoklás."""
        
        schema = {
            "type": "object",
            "required": ["verdict"],
            "properties": {
                "verdict": {"type": "string", "enum": ["APPROVE", "REJECT"]},
                "reason": {"type": "string", "maxLength": 200},
                "additional_issues": {"type": "array", "items": {"type": "string"}}
            }
        }
        
        try:
            response = self.gateway.call(
                "qa", "llama-3.3-70b-versatile",
                [{"role": "user", "content": prompt_text}],
                max_output_tokens=300,
                schema=schema
            )
            data = response.get("data", {})
            
            additional = []
            for issue in data.get("additional_issues", [])[:3]:
                additional.append({
                    "severity": "medium",
                    "type": "llm_review",
                    "message": issue
                })
            
            return {
                "additional_findings": additional,
                "confidence": 0.9 if data.get("verdict") == "APPROVE" else 0.5,
                "tokens": response.get("tokens", 0)
            }
        except Exception as e:
            return {}
