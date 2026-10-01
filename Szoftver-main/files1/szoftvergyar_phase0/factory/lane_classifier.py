"""Lane Classifier (1. fázis): a feladatok S1/S2 sávba sorolása.

A klasszifikáció egyszerű, determinisztikus és nem LLM-függő. A cél az, hogy
minden feladat kapjon egy alapszintű kockázati besorolást a Supervisor számára.

S1 = kis és alacsony kockázatú feladat (docs, lint, typo, small fix)
S2 = nagyobb/kockázatosabb feladat (security, refactor, schema, infra, deploy)
"""
from __future__ import annotations

import json
import os
from typing import Any, Mapping


class LaneClassifier:
    """S1 = kis és alacsony kockázatú feladat, S2 = nagyobb/kockázatosabb feladat."""

    S1_HINTS = (
        "typo",
        "spelling",
        "format",
        "doc",
        "docs",
        "documentation",
        "readme",
        "comment",
        "lint",
        "style",
        "small fix",
        "trivial",
        "rename",
        "copy",
        "message",
        "error text",
        "fix warning",
        "whitespace",
        "formatting",
    )

    S2_HINTS = (
        "security",
        "auth",
        "authentication",
        "authorization",
        "permission",
        "privilege",
        "secret",
        "token",
        "db",
        "database",
        "migrate",
        "migration",
        "refactor",
        "rewrite",
        "architecture",
        "schema",
        "pipeline",
        "infra",
        "infrastructure",
        "deploy",
        "deployment",
        "ops",
        "operations",
        "gateway",
        "sandbox",
        "audit",
        "state",
        "performance",
        "critical",
        "high-risk",
    )

    def classify(self, task: Mapping[str, Any] | str | None, *, default: str = "S2") -> str:
        """Determinista osztályozás: 'S1' vagy 'S2'."""
        if task is None:
            return default
        if isinstance(task, str):
            data = {"text": task}
        elif isinstance(task, Mapping):
            data = dict(task)
        else:
            data = {"text": str(task)}

        text = self._collect_text(data)
        text_l = text.lower()

        # Közvetlen S2 / a kockázat tiltása
        for hint in self.S2_HINTS:
            if hint in text_l:
                return "S2"

        # S1-es jelek: kis hatókör, egyszerű javítások, dokumentáció, egy-fájl fixek
        s1_score = 0
        for hint in self.S1_HINTS:
            if hint in text_l:
                s1_score += 1

        files = self._as_list(data.get("files") or data.get("paths") or data.get("affected_files"))
        file_count = len(files)
        if file_count == 0:
            file_count = 1

        if file_count <= 2:
            s1_score += 1

        if data.get("risk") in ("low", "tiny", "minor"):
            s1_score += 1

        if data.get("kind") in ("bugfix", "fix", "hotfix", "typo", "docs"):
            s1_score += 1

        if data.get("size") in ("small", "tiny", "minimal"):
            s1_score += 1

        # Döntés: S1 csak ha van elég S1-es jel, és nincs S2-es jel.
        if s1_score >= 2:
            return "S1"

        # Alapértelmezett: kockázatosabb feladatok S2-be kerülnek.
        return default if default in {"S1", "S2"} else "S2"

    def classify_task(self, task: Mapping[str, Any] | str | None, *, default: str = "S2") -> str:
        """Publikus interfész a feladat besorolásához."""
        return self.classify(task, default=default)

    def __call__(self, task: Mapping[str, Any] | str | None, *, default: str = "S2") -> str:
        """Callable interfész."""
        return self.classify(task, default=default)

    @staticmethod
    def _collect_text(data: Mapping[str, Any]) -> str:
        """Szöveg összegyűjtése az összes releváns mezőből."""
        chunks: list[str] = []
        for key in ("title", "summary", "description", "text", "prompt", "spec", "message", "reason", "requirement"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                chunks.append(value)
        if "details" in data and isinstance(data["details"], Mapping):
            chunks.append(LaneClassifier._collect_text(data["details"]))
        if "files" in data and isinstance(data["files"], (list, tuple)):
            chunks.extend(str(x) for x in data["files"])
        return "\n".join(chunks)

    @staticmethod
    def _as_list(value: Any) -> list[str]:
        """Érték normalizálása listává."""
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            return [str(x) for x in value]
        return [str(value)]


DEFAULT_LANE_CLASSIFIER = LaneClassifier()


if __name__ == "__main__":
    print("S1:", DEFAULT_LANE_CLASSIFIER.classify("Fix typo in README"))
    print("S2:", DEFAULT_LANE_CLASSIFIER.classify("Add OAuth login flow with security checks"))
    print("S1:", DEFAULT_LANE_CLASSIFIER.classify({"text": "Update docs", "size": "small"}))
    print("S2:", DEFAULT_LANE_CLASSIFIER.classify({"text": "Refactor auth module", "files": ["auth.py", "token.py", "session.py"]}))
