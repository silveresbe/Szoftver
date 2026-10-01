"""Determinisztikus kapuk (factory/gates.py) – hamis sandboxszal, 0 token, Docker nélkül."""
import copy, json, os, tempfile, unittest
from factory import config as C
from factory.audit import AuditLog
from factory.gates import GateRunner, GatesConfigError, GateToolsMissing, StageTooLarge, error_lines, fingerprint, stage_tree
from factory.killswitch import KillSwitch, Killed
from factory.sandbox import SandboxRefused
from tests.test_product_owner import CFG


class FakeSandbox:
    """A run() hívásokat rögzíti, a szakaszolt fa tartalmával együtt; a válaszokat sorban adja ((rc, out) vagy kivétel)."""
    def __init__(self, replies=()):
        self.replies, self.calls = list(replies), []

    def run(self, argv, worktree=None):
        listing = sorted(os.path.relpath(os.path.join(c, f), worktree).replace("\\", "/") for c, _, fs in os.walk(worktree) for f in fs) if worktree else None
        self.calls.append({"argv": list(argv), "files": listing})
        r = self.replies.pop(0) if self.replies else (0, "")
        if isinstance(r, Exception):
            raise r
        return r


def tree(d, files):
    for rel, text in files.items():
        p = os.path.join(d, rel); os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f: f.write(text)


class GateBase(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(); self.root = os.path.join(self.d, "repo"); os.makedirs(self.root)
        self.cfg = copy.deepcopy(CFG); self.audit = AuditLog(os.path.join(self.d, "audit", "d.jsonl"))
        tree(self.root, {"src/a.py": "x = 1\n", "tests/test_a.py": "def test(): pass\n"})

    def runner(self, replies=(), kill=None):
        self.sb = FakeSandbox(replies)
        return GateRunner(self.cfg, self.sb, self.audit, self.root, kill=kill)

    def audit_text(self):
        with open(self.audit.path, encoding="utf-8") as f: return f.read()


class TestRunner(GateBase):
    def test_all_green_runs_every_gate_in_order(self):
        r = self.runner().run("T-1")
        self.assertTrue(r["ok"]); self.assertEqual([c["argv"][2] for c in self.sb.calls], ["compileall", "unittest"])
        self.assertEqual([x["name"] for x in r["results"]], ["syntax", "unit_tests"]); self.assertIn("GATES_GREEN", self.audit_text())

    def test_first_failing_gate_stops_the_rest(self):
        r = self.runner([(1, "  File \"src/a.py\", line 3\nSyntaxError: invalid syntax\n")]).run("T-1")
        self.assertFalse(r["ok"]); self.assertEqual(r["gate"], "syntax"); self.assertEqual(len(self.sb.calls), 1)
        self.assertIn("SyntaxError", r["feedback"]); self.assertIn("syntax", r["feedback"])

    def test_second_gate_failure_reports_second_gate(self):
        r = self.runner([(0, ""), (1, "FAIL: test_ok (tests.test_a)\nAssertionError: 1 != 2\n")]).run("T-1")
        self.assertEqual(r["gate"], "unit_tests"); self.assertEqual(len(self.sb.calls), 2)

    def test_timeout_rc_124_gets_fixed_message(self):
        r = self.runner([(124, "sandbox timeout")]).run("T-1"); self.assertEqual(r["errors"], ["időtúllépés a sandboxban"])

    def test_audit_has_no_output_text(self):
        self.runner([(1, "AssertionError: TITOK_KIMENET_123\n")]).run("T-1"); t = self.audit_text()
        self.assertIn("GATE_FAILED", t); self.assertNotIn("TITOK_KIMENET_123", t)

    def test_sandbox_refused_propagates(self):
        with self.assertRaises(SandboxRefused): self.runner([SandboxRefused("nincs runtime")]).run("T-1")

    def test_kill_switch_stops_before_gate(self):
        k = KillSwitch(os.path.join(self.d, "state")); k.trip("teszt")
        r = self.runner(kill=k)
        with self.assertRaises(Killed): r.run("T-1")
        self.assertEqual(self.sb.calls, [])

    def test_no_gates_configured_is_error(self):
        self.cfg["ci_pipeline"]["gates"] = []
        with self.assertRaises(GatesConfigError): self.runner()

    def test_argv_is_passed_as_list_without_shell(self):
        self.runner().run("T-1"); self.assertIsInstance(self.sb.calls[0]["argv"], list); self.assertNotIn("sh", self.sb.calls[0]["argv"][:1])


class TestStaging(GateBase):
    def test_forbidden_paths_git_and_links_never_reach_sandbox(self):
        tree(self.root, {".env": "K=1\n", "secrets/k.txt": "s", "state/f.db": "d", "audit/d.jsonl": "{}", "evals/recordings/r.json": "{}",
                         "keys/server.pem": "p", ".git/config": "c", "node_modules/x/i.js": "1", "src/__pycache__/a.pyc": "b"})
        os.symlink("/etc/passwd", os.path.join(self.root, "src", "link.py")); os.symlink("/etc", os.path.join(self.root, "etcdir"))
        self.runner().run("T-1"); got = self.sb.calls[0]["files"]
        self.assertEqual(got, ["src/a.py", "tests/test_a.py"])

    def test_extra_exclude_from_config(self):
        tree(self.root, {"big/data.txt": "x", "docs/a.md": "y"}); self.cfg["ci_pipeline"]["staging"]["exclude"] = ["big"]
        self.runner().run("T-1"); self.assertNotIn("big/data.txt", self.sb.calls[0]["files"]); self.assertIn("docs/a.md", self.sb.calls[0]["files"])

    def test_too_large_tree_raises_and_nothing_runs(self):
        with open(os.path.join(self.root, "src", "big.bin"), "wb") as f: f.write(b"0" * (2 * 1024 * 1024))
        self.cfg["ci_pipeline"]["staging"]["max_mb"] = 1; r = self.runner()
        with self.assertRaises(StageTooLarge): r.run("T-1")
        self.assertEqual(self.sb.calls, [])

    def test_file_mode_is_preserved(self):
        p = os.path.join(self.root, "src", "run.sh"); tree(self.root, {"src/run.sh": "#!/bin/sh\n"}); os.chmod(p, 0o755)
        dest = tempfile.mkdtemp(); stage_tree(self.root, dest, 10 ** 6)
        self.assertTrue(os.access(os.path.join(dest, "src", "run.sh"), os.X_OK))

    def test_original_tree_is_untouched(self):
        before = open(os.path.join(self.root, "src", "a.py")).read(); self.runner().run("T-1")
        self.assertEqual(open(os.path.join(self.root, "src", "a.py")).read(), before); self.assertFalse(os.path.exists(os.path.join(self.root, "src", "__pycache__")))

    def test_stage_dir_is_removed_after_run(self):
        seen = []; r = self.runner(); orig = self.sb.run
        self.sb.run = lambda argv, worktree=None: (seen.append(worktree), orig(argv, worktree))[1]; r.run("T-1")
        self.assertTrue(seen and not os.path.exists(seen[0]))


class TestParsing(unittest.TestCase):
    def test_failure_markers_are_picked(self):
        out = "Ran 3 tests\n.F.\n======\nFAIL: test_x (t.T)\nTraceback (most recent call last):\n  File \"t.py\", line 5, in test_x\nAssertionError: 1 != 2\nOK-ish noise\n"
        e = error_lines(out); self.assertIn("FAIL: test_x (t.T)", e); self.assertIn("AssertionError: 1 != 2", e); self.assertNotIn("OK-ish noise", e)

    def test_no_marker_falls_back_to_tail(self):
        out = "\n".join(f"sor {i}" for i in range(50)); e = error_lines(out); self.assertEqual(e[-1], "sor 49"); self.assertEqual(len(e), 10)

    def test_capped_deduplicated_and_truncated(self):
        out = "\n".join([f"ERROR: e{i}" for i in range(80)] + ["ERROR: e1"] + ["ERROR: " + "x" * 500])
        e = error_lines(out); self.assertLessEqual(len(e), 30); self.assertEqual(len(set(e)), len(e))
        self.assertTrue(all(len(l) <= 200 for l in error_lines("ERROR: " + "x" * 500)))

    def test_fingerprint_ignores_numbers_addresses_timing_and_tmp_paths(self):
        a = ["File \"/tmp/fsz-stage-ab12/x.py\", line 12", "AssertionError: 1 != 2 (0.31s)", "obj at 0x7f00aa"]
        b = ["File \"/tmp/fsz-stage-cd34/x.py\", line 99", "AssertionError: 7 != 8 (12ms)", "obj at 0x7f11bb"]
        self.assertEqual(fingerprint("unit_tests", a), fingerprint("unit_tests", b))

    def test_fingerprint_differs_by_message_and_gate_and_ignores_order(self):
        self.assertNotEqual(fingerprint("g", ["AssertionError: A"]), fingerprint("g", ["AssertionError: B"]))
        self.assertNotEqual(fingerprint("g1", ["E"]), fingerprint("g2", ["E"]))
        self.assertEqual(fingerprint("g", ["A", "B"]), fingerprint("g", ["B", "A"]))


class TestGateConfig(unittest.TestCase):
    def cfg(self): return copy.deepcopy(CFG)
    def bad(self, mut):
        c = self.cfg(); mut(c["ci_pipeline"])
        with self.assertRaises(C.ConfigError): C.validate_deep(C.validate(c))

    def test_sample_ok(self): C.validate_deep(C.validate(self.cfg()))
    def test_empty_gates(self): self.bad(lambda ci: ci.update(gates=[]))
    def test_missing_name(self): self.bad(lambda ci: ci["gates"][0].pop("name"))
    def test_duplicate_name(self): self.bad(lambda ci: ci["gates"].append(dict(ci["gates"][0])))
    def test_empty_argv(self): self.bad(lambda ci: ci["gates"][0].update(argv=[]))
    def test_non_string_argv(self): self.bad(lambda ci: ci["gates"][0].update(argv=["python", 3]))
    def test_shell_dash_c_forbidden(self): self.bad(lambda ci: ci["gates"][0].update(argv=["sh", "-c", "true"]))
    def test_gate_cannot_be_soft(self): self.bad(lambda ci: ci["gates"][0].update(hard=False))
    def test_staging_bad_size(self): self.bad(lambda ci: ci["staging"].update(max_mb=0))
    def test_staging_bad_exclude(self): self.bad(lambda ci: ci["staging"].update(exclude="big"))

if __name__ == "__main__": unittest.main()


TOOL_GATES = [{"name": "syntax", "argv": ["python", "-m", "compileall", "-q", "."]},
              {"name": "lint", "argv": ["python", "-m", "ruff", "check", "."], "requires": ["ruff"]},
              {"name": "types", "argv": ["python", "-m", "mypy", "."], "requires": ["mypy"]}]


class TestRequiredTools(GateBase):
    def setUp(self):
        super().setUp(); self.cfg["ci_pipeline"] = {"gates": copy.deepcopy(TOOL_GATES), "staging": {"max_mb": 50, "exclude": []}}

    def test_preflight_runs_once_before_staging_and_lists_all_modules(self):
        r = self.runner().run("T-1")
        self.assertTrue(r["ok"]); first = self.sb.calls[0]
        self.assertIsNone(first["files"]); self.assertEqual(first["argv"][:2], ["python", "-c"]); self.assertEqual(first["argv"][3:], ["mypy", "ruff"])
        self.assertEqual(len(self.sb.calls), 1 + 3)

    def test_preflight_is_cached_per_image(self):
        gr = self.runner(); gr.sandbox.c = {"image": "a"}
        gr.run("T-1"); n = len(self.sb.calls); gr.run("T-2")
        self.assertEqual(len(self.sb.calls) - n, 3)                      # másodszor nincs előzetes ellenőrzés
        gr.sandbox.c = {"image": "b"}; n = len(self.sb.calls); gr.run("T-3")
        self.assertEqual(len(self.sb.calls) - n, 4)                      # képváltás: újra ellenőriz

    def test_missing_tool_raises_and_no_gate_runs(self):
        gr = self.runner([(1, "missing: ruff\n")])
        with self.assertRaises(GateToolsMissing) as cm: gr.run("T-1")
        self.assertIn("ruff", str(cm.exception)); self.assertEqual(len(self.sb.calls), 1)
        self.assertIn("GATE_TOOLS_MISSING", self.audit_text()); self.assertNotIn("GATES_GREEN", self.audit_text())

    def test_failed_preflight_is_not_cached(self):
        gr = self.runner([(1, "missing: mypy\n")])
        with self.assertRaises(GateToolsMissing): gr.run("T-1")
        self.assertTrue(gr.run("T-1")["ok"])                              # a második próbán az eszköz már megvan

    def test_kill_switch_stops_before_preflight(self):
        ks = KillSwitch(os.path.join(self.d, "KILL")); ks.trip("teszt")
        with self.assertRaises(Killed): self.runner(kill=ks).run("T-1")
        self.assertEqual(self.sb.calls, [])

    def test_no_requires_means_no_preflight(self):
        self.cfg["ci_pipeline"]["gates"] = [dict(TOOL_GATES[0])]
        self.runner().run("T-1"); self.assertEqual(len(self.sb.calls), 1)

    def test_lint_failure_feedback_keeps_file_and_line(self):
        r = self.runner([(0, ""), (0, ""), (1, "src/a.py:3:1: F401 `os` imported but unused\n")]).run("T-1")
        self.assertEqual(r["gate"], "lint"); self.assertIn("src/a.py:3:1", r["feedback"])


class TestShippedYamlGates(unittest.TestCase):
    def test_yaml_gates_valid_and_ordered_cheap_first(self):
        import yaml
        with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "factory.yaml"), encoding="utf-8") as f: y = yaml.safe_load(f)
        C.validate_deep(C.validate(y))
        gs = {g["name"]: g for g in y["ci_pipeline"]["gates"]}
        self.assertEqual([g["name"] for g in y["ci_pipeline"]["gates"]], ["syntax", "lint", "types", "unit_tests", "lint_soft"])
        self.assertEqual(gs["lint"]["requires"], ["ruff"]); self.assertEqual(gs["types"]["requires"], ["mypy"])

    def test_gate_image_matches_dockerfile_tools(self):
        root = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(root, "docker", "gates.Dockerfile"), encoding="utf-8") as f: df = f.read()
        self.assertIn("ruff==", df); self.assertIn("mypy==", df)


class TestRequiresConfig(unittest.TestCase):
    def bad(self, mut):
        cfg = copy.deepcopy(CFG); mut(cfg["ci_pipeline"]["gates"][0])
        with self.assertRaises(C.ConfigError): C.validate_deep(C.validate(cfg))

    def test_requires_must_be_nonempty_list_of_module_names(self):
        for v in ([], "ruff", [3], ["ru ff"], ["../x"], [""]):
            self.bad(lambda g, v=v: g.update(requires=v))

    def test_requires_ok(self):
        cfg = copy.deepcopy(CFG); cfg["ci_pipeline"]["gates"][0]["requires"] = ["ruff", "a.b"]; C.validate_deep(C.validate(cfg))
