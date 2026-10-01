"""Autofix, puha kapu, adósság-nyilvántartás (11.2) – hamis sandboxszal, 0 token, Docker és ruff nélkül."""
import copy, json, os, subprocess, sys, tempfile, textwrap, unittest
from factory import autofix as A, config as C
from factory.audit import AuditLog
from factory.autofix import Autofixer, DRIVER, BEGIN, END, parse_payload, touched_lines
from factory.debt import DebtRegister
from factory.gates import GateRunner, parse_findings
from factory.killswitch import KillSwitch, Killed
from factory.sandbox import SandboxRefused
from factory.tool_gateway import ToolGateway
from tests.test_drive import DriveBase, GREEN, FAIL_A
from tests.test_gates import FakeSandbox, tree
from tests.test_master_coder import GOOD_PATCH, IMPL, out, edit
from tests.test_product_owner import CFG

SOFT_GATE = {"name": "lint_soft", "kind": "soft", "argv": ["python", "-m", "ruff", "check", "--select", "E,W,I", "."]}
SOFT_SECTION = {"enabled": True, "return_soft_to_coder_until_iteration": 1, "max_soft_findings_returned": 5, "result_status": "GREEN_WITH_DEBT",
                "debt_intake": {"register": "state/tech_debt.jsonl", "max_open_items": 30}}
E501 = lambda n=1, f="src/coupon.py": "".join(f"{f}:{i}:1: E501 Line too long ({100 + i} > 88)\n" for i in range(1, n + 1))


def payload(d):
    return BEGIN + json.dumps(d) + END


class AfSandbox(FakeSandbox):
    """Az autofix-hívást (DRIVER) a szakaszolt fán `transform`-mal hajtja végre, a valódi driver kimeneti formátumával; a többi hívás a válaszsor."""
    def __init__(self, replies=(), transform=None, raw=None):
        super().__init__(replies); self.transform, self.raw, self.af_calls = transform, raw, []

    def run(self, argv, worktree=None):
        if argv[:2] == ["python", "-c"] and argv[2] == DRIVER:
            self.af_calls.append({"argv": list(argv), "files": sorted(os.path.relpath(os.path.join(c, f), worktree).replace("\\", "/") for c, _, fs in os.walk(worktree) for f in fs)})
            if self.raw is not None:
                r = self.raw
                if isinstance(r, Exception): raise r
                return r
            ch = {}
            for rel in argv[3:]:
                with open(os.path.join(worktree, rel), encoding="utf-8", newline="") as f: old = f.read()
                new = self.transform(rel, old) if self.transform else old
                if new != old:
                    import hashlib
                    ch[rel] = {"sha_before": hashlib.sha256(old.encode()).hexdigest(), "text": new}
            return 0, "ruff: ok\n" + payload(ch)
        return super().run(argv, worktree)


dq = lambda rel, t: t.replace("'", '"')                  # „formázó”: idézőjel-csere


class AfBase(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(); self.root = os.path.join(self.d, "repo"); os.makedirs(self.root)
        self.cfg = copy.deepcopy(CFG); self.cfg["ci_pipeline"]["autofix"] = {"enabled": True, "max_touched_lines_outside_diff": 30}
        self.audit = AuditLog(os.path.join(self.d, "audit", "d.jsonl")); self.kill = KillSwitch(os.path.join(self.d, "state"))
        self.tools = ToolGateway(self.cfg, self.audit, self.kill)
        tree(self.root, {"src/a.py": "x = 'a'\ny = 'b'\n", "docs/n.md": "'x'\n", "tests/acceptance/t.py": "z = 'q'\n"})

    def af(self, **kw):
        self.sb = AfSandbox(**kw)
        return Autofixer(self.cfg, self.sb, self.tools, self.audit, self.root, kill=self.kill)

    def r(self, rel):
        with open(os.path.join(self.root, rel), encoding="utf-8", newline="") as f: return f.read()

    def audit_text(self):
        with open(self.audit.path, encoding="utf-8") as f: return f.read()


class TestAutofixer(AfBase):
    def test_fixes_file_on_disk(self):
        r = self.af(transform=dq).run("T-1", ["src/a.py"], created=["src/a.py"])
        self.assertEqual(r["status"], "FIXED"); self.assertEqual(r["files"], ["src/a.py"]); self.assertEqual(self.r("src/a.py"), 'x = "a"\ny = "b"\n')
        self.assertIn("AUTOFIX_APPLIED", self.audit_text()); self.assertNotIn('"a"', self.audit_text())

    def test_nothing_to_fix_is_noop_and_leaves_tree(self):
        r = self.af().run("T-1", ["src/a.py"]); self.assertEqual(r["status"], "NOOP"); self.assertEqual(self.r("src/a.py"), "x = 'a'\ny = 'b'\n")

    def test_only_python_files_non_protected(self):
        af = self.af(transform=dq)
        self.assertEqual(af.run("T-1", ["docs/n.md", "tests/acceptance/t.py", "missing.py", "../x.py", "/etc/p.py"])["status"], "NOOP")
        self.assertEqual(self.sb.af_calls, []); self.assertEqual(self.r("tests/acceptance/t.py"), "z = 'q'\n")

    def test_symlink_target_is_skipped(self):
        os.symlink(os.path.join(self.root, "src", "a.py"), os.path.join(self.root, "src", "ln.py"))
        self.assertEqual(self.af(transform=dq).run("T-1", ["src/ln.py"])["status"], "NOOP")

    def test_sandbox_gets_only_requested_files_and_no_forbidden_paths(self):
        tree(self.root, {"state/s.db": "x", ".env": "K=1", "src/b.py": "q = 1\n"})
        af = self.af(transform=dq); af.run("T-1", ["src/a.py"])
        self.assertEqual(self.sb.af_calls[0]["argv"][3:], ["src/a.py"]); self.assertNotIn("state/s.db", self.sb.af_calls[0]["files"]); self.assertNotIn(".env", self.sb.af_calls[0]["files"])

    def test_unexpected_path_from_sandbox_skips_everything(self):
        raw = (0, payload({"src/a.py": {"sha_before": "x", "text": "1"}, "src/other.py": {"sha_before": "x", "text": "2"}}))
        r = self.af(raw=raw).run("T-1", ["src/a.py"]); self.assertEqual((r["status"], r["why"]), ("SKIPPED", "unexpected_path")); self.assertEqual(self.r("src/a.py"), "x = 'a'\ny = 'b'\n")

    def test_file_changed_meanwhile_skips(self):
        raw = (0, payload({"src/a.py": {"sha_before": "0" * 64, "text": "x = 1\n"}}))
        r = self.af(raw=raw).run("T-1", ["src/a.py"]); self.assertEqual(r["why"], "file_changed_meanwhile"); self.assertEqual(self.r("src/a.py"), "x = 'a'\ny = 'b'\n")

    def test_bad_outputs_skip_safely(self):
        for raw, why in (((1, "boom"), "sandbox_rc"), ((0, "nincs jelölő"), "no_or_truncated_output"), ((0, BEGIN + '{"src/a.py": {"te'), "no_or_truncated_output"),
                         ((0, BEGIN + "[1]" + END), "no_or_truncated_output"), ((0, "@@FSZ-AUTOFIX-TOOBIG@@\n"), "payload_too_big")):
            r = self.af(raw=raw).run("T-1", ["src/a.py"]); self.assertEqual((r["status"], r["why"]), ("SKIPPED", why), raw)
            self.assertEqual(self.r("src/a.py"), "x = 'a'\ny = 'b'\n")
        self.assertIn("AUTOFIX_SKIPPED", self.audit_text())

    def test_too_wide_outside_diff_is_skipped_but_created_file_is_not(self):
        big = "".join(f"v{i} = 'x'\n" for i in range(40)); tree(self.root, {"src/big.py": big})
        r = self.af(transform=dq).run("T-1", ["src/big.py"]); self.assertEqual((r["status"], r["why"]), ("SKIPPED", "too_wide")); self.assertEqual(self.r("src/big.py"), big)
        r = self.af(transform=dq).run("T-1", ["src/big.py"], created=["src/big.py"]); self.assertEqual(r["status"], "FIXED"); self.assertNotIn("'", self.r("src/big.py"))

    def test_limit_comes_from_config(self):
        self.cfg["ci_pipeline"]["autofix"]["max_touched_lines_outside_diff"] = 1
        self.assertEqual(self.af(transform=dq).run("T-1", ["src/a.py"])["why"], "too_wide")

    def test_disabled(self):
        self.cfg["ci_pipeline"]["autofix"]["enabled"] = False
        r = self.af(transform=dq).run("T-1", ["src/a.py"]); self.assertEqual(r["status"], "DISABLED"); self.assertEqual(self.sb.af_calls, [])

    def test_kill_switch_stops_before_sandbox(self):
        self.kill.trip("x"); af = self.af(transform=dq)
        with self.assertRaises(Killed): af.run("T-1", ["src/a.py"])
        self.assertEqual(self.sb.af_calls, [])

    def test_sandbox_refused_propagates(self):
        with self.assertRaises(SandboxRefused): self.af(raw=SandboxRefused("x")).run("T-1", ["src/a.py"])

    def test_write_goes_through_tool_gateway_protected_zone(self):
        self.tools.protected = list(self.tools.protected) + ["src/a.py"]
        self.assertEqual(self.af(transform=dq).run("T-1", ["src/a.py"])["status"], "NOOP"); self.assertEqual(self.r("src/a.py"), "x = 'a'\ny = 'b'\n")

    def test_autofix_identity_has_minimal_permissions(self):
        self.assertEqual(self.tools.perms["autofix"], {"repo_read", "repo_write"})


class TestDriverAndParsing(unittest.TestCase):
    def test_touched_lines(self):
        self.assertEqual(touched_lines("a\nb\nc\n", "a\nb\nc\n"), 0); self.assertEqual(touched_lines("a\nb\nc\n", "a\nX\nc\n"), 1)
        self.assertEqual(touched_lines("a\n", "a\nb\nc\n"), 2)

    def test_driver_uses_safe_fixes_only_and_is_valid_python(self):
        compile(DRIVER, "driver", "exec")
        for bad in ("--unsafe-fixes", "--select ALL"): self.assertNotIn(bad, DRIVER)
        self.assertIn('"--fix"', DRIVER); self.assertIn("F,E,W,UP,SIM,I", DRIVER)

    def test_driver_end_to_end_with_fake_ruff(self):
        d = tempfile.mkdtemp(); os.makedirs(os.path.join(d, "pk", "ruff"))
        with open(os.path.join(d, "pk", "ruff", "__init__.py"), "w") as f: f.write("")
        with open(os.path.join(d, "pk", "ruff", "__main__.py"), "w") as f:
            f.write(textwrap.dedent('''
                import sys
                a = sys.argv[1:]
                files = a[a.index("--") + 1:]
                if a[0] == "format":
                    for p in files:
                        t = open(p, newline="").read(); open(p, "w", newline="").write(t.replace("'", '"'))
                sys.exit(1 if a[0] == "check" else 0)      # a check rc-jét a driver nem használja (--exit-zero)
            '''))
        with open(os.path.join(d, "a.py"), "w") as f: f.write("x = 'a'\n")
        with open(os.path.join(d, "b.py"), "w") as f: f.write('y = "b"\n')
        p = subprocess.run([sys.executable, "-c", DRIVER, "a.py", "b.py"], cwd=d, capture_output=True, text=True, env={**os.environ, "PYTHONPATH": os.path.join(d, "pk")})
        self.assertEqual(p.returncode, 0, p.stderr); ch = parse_payload(p.stdout)
        self.assertEqual(list(ch), ["a.py"]); self.assertEqual(ch["a.py"]["text"], 'x = "a"\n'); self.assertEqual(len(ch["a.py"]["sha_before"]), 64)

    def test_parse_payload_takes_last_complete_block(self):
        self.assertEqual(parse_payload("zaj\n" + payload({"a": 1}) + "\n"), {"a": 1}); self.assertIsNone(parse_payload(""))
        self.assertIsNone(parse_payload(None))


class TestDebtRegister(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(); self.audit = AuditLog(os.path.join(self.d, "audit", "d.jsonl"))
        self.path = os.path.join(self.d, "state", "tech_debt.jsonl"); self.reg = DebtRegister(self.path, 3, self.audit)

    F = lambda self, rule="E501", file="a.py", line=1: {"rule": rule, "file": file, "line": line, "message": "Line too long SECRETTEXT"}

    def test_add_records_with_origin_and_task(self):
        self.assertEqual(self.reg.add("T-1", [self.F()]), {"added": 1, "duplicates": 0})
        it = self.reg.open_items()[0]; self.assertEqual((it["origin"], it["task_id"], it["rule"], it["status"]), ("soft_gate", "T-1", "E501", "open"))

    def test_dedupe_by_rule_and_file_across_calls_and_within_batch(self):
        self.reg.add("T-1", [self.F(), self.F(line=9), self.F("W291")])
        self.assertEqual(self.reg.open_count(), 2); self.assertEqual(self.reg.add("T-2", [self.F(line=50)]), {"added": 0, "duplicates": 1})
        self.assertEqual(self.reg.open_count(), 2)

    def test_full_list_closes_gate_and_add_raises(self):
        self.reg.add("T-1", [self.F(file=f"{i}.py") for i in range(3)])
        self.assertFalse(self.reg.fits([self.F(file="new.py")])); self.assertTrue(self.reg.fits([self.F(file="0.py")]))   # duplikátum belefér
        with self.assertRaises(ValueError): self.reg.add("T-2", [self.F(file="new.py")])
        self.assertEqual(self.reg.open_count(), 3)

    def test_closing_items_reopens_the_gate(self):
        self.reg.add("T-1", [self.F(file=f"{i}.py") for i in range(3)]); ids = [i["id"] for i in self.reg.open_items()]
        self.assertEqual(self.reg.close(ids[:1] + ["nincs"]), 1); self.assertTrue(self.reg.fits([self.F(file="new.py")])); self.assertEqual(self.reg.close(ids[:1]), 0)

    def test_audit_has_counts_and_rules_but_not_message_text(self):
        self.reg.add("T-1", [self.F()])
        with open(self.audit.path, encoding="utf-8") as f: t = f.read()
        self.assertIn("DEBT_RECORDED", t); self.assertIn("E501", t); self.assertNotIn("SECRETTEXT", t)

    def test_corrupt_line_is_ignored_not_invented(self):
        self.reg.add("T-1", [self.F()])
        with open(self.path, "a") as f: f.write("{nem json\n")
        self.assertEqual(self.reg.open_count(), 1); self.reg.add("T-2", [self.F("W291")]); self.assertEqual(self.reg.open_count(), 2)

    def test_state_dir_is_protected_from_agents(self):
        tg = ToolGateway(copy.deepcopy(CFG), self.audit)
        from factory.tool_gateway import ToolDenied
        with self.assertRaises(ToolDenied): tg.authorize("master_coder", "repo_write", "state/tech_debt.jsonl")


class TestSoftGateRunner(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(); self.root = os.path.join(self.d, "repo"); os.makedirs(self.root); tree(self.root, {"src/a.py": "x = 1\n"})
        self.cfg = copy.deepcopy(CFG); self.cfg["ci_pipeline"]["gates"] = self.cfg["ci_pipeline"]["gates"][:1] + [copy.deepcopy(SOFT_GATE)] + self.cfg["ci_pipeline"]["gates"][1:]
        self.audit = AuditLog(os.path.join(self.d, "audit", "d.jsonl"))

    def run_(self, replies):
        self.sb = FakeSandbox(replies); return GateRunner(self.cfg, self.sb, self.audit, self.root).run("T-1")

    def test_soft_findings_do_not_stop_later_gates(self):
        r = self.run_([(0, ""), (1, E501(2)), (0, "")])
        self.assertTrue(r["ok"]); self.assertEqual(len(self.sb.calls), 3); self.assertEqual([f["rule"] for f in r["soft"]], ["E501", "E501"])
        self.assertEqual(r["soft"][0]["file"], "src/coupon.py")

    def test_clean_soft_gate_has_empty_soft_list(self):
        self.assertEqual(self.run_([(0, ""), (0, ""), (0, "")])["soft"], [])

    def test_unparseable_soft_output_is_a_hard_failure(self):
        r = self.run_([(0, ""), (1, "ruff exploded\n")]); self.assertFalse(r["ok"]); self.assertEqual(r["gate"], "lint_soft")

    def test_soft_gate_tool_error_rc2_is_a_hard_failure(self):
        r = self.run_([(0, ""), (2, E501(1))]); self.assertFalse(r["ok"]); self.assertEqual(r["gate"], "lint_soft")

    def test_hard_gate_failure_still_stops(self):
        r = self.run_([(1, "SyntaxError: x\n")]); self.assertFalse(r["ok"]); self.assertEqual(r["gate"], "syntax")

    def test_a_failing_test_gate_after_soft_gate_wins(self):
        r = self.run_([(0, ""), (1, E501(1)), (1, "FAIL: t\nAssertionError: x\n")]); self.assertFalse(r["ok"]); self.assertEqual(r["gate"], "unit_tests")

    def test_parse_findings_formats(self):
        f = parse_findings("./src/a.py:3:1: F401 [*] `os` imported but unused\nAll checks passed!\nfoo\nsrc/a.py:3:1: F401 [*] `os` imported but unused\n")
        self.assertEqual(f, [{"file": "src/a.py", "line": 3, "rule": "F401", "message": "`os` imported but unused"}])


class TestSoftGateConfig(unittest.TestCase):
    def cfg(self, **gate):
        c = copy.deepcopy(CFG); c["ci_pipeline"]["gates"].append({**copy.deepcopy(SOFT_GATE), **gate}); c["ci_pipeline"]["soft_gate"] = copy.deepcopy(SOFT_SECTION); return c

    def ok(self, c): C.validate_deep(C.validate(c))

    def bad(self, c):
        with self.assertRaises(C.ConfigError): self.ok(c)

    def test_sample_ok(self): self.ok(self.cfg())
    def test_soft_requires_enabled_section(self):
        c = self.cfg(); c["ci_pipeline"]["soft_gate"]["enabled"] = False; self.bad(c)
        c = self.cfg(); del c["ci_pipeline"]["soft_gate"]; self.bad(c)
    def test_only_ruff_check_can_be_soft(self):
        for av in (["python", "-m", "unittest"], ["python", "-m", "mypy", "."], ["python", "-m", "compileall", "."], ["sh", "x"],
                   ["python", "-m", "mypy", "--select", "E", "."], ["python", "-m", "pytest", "--select", "E", "."], ["ruff", "check", "--select", "E", "."]): self.bad(self.cfg(argv=av))
    def test_hard_rule_groups_cannot_be_soft(self):
        for sel in ("F", "E,F", "E9", "E902", "S", "B", "PLE", "ALL", "", "E,,W"): self.bad(self.cfg(argv=["python", "-m", "ruff", "check", "--select", sel, "."]))
    def test_select_is_required(self): self.bad(self.cfg(argv=["python", "-m", "ruff", "check", "."]))
    def test_select_equals_form_accepted(self): self.ok(self.cfg(argv=["python", "-m", "ruff", "check", "--select=E,W", "."]))
    def test_masking_flags_forbidden(self):
        for fl in ("--fix", "--unsafe-fixes", "--exit-zero", "--ignore", "--extend-ignore", "--per-file-ignores=x:E"):
            self.bad(self.cfg(argv=["python", "-m", "ruff", "check", "--select", "E", fl, "."]))
    def test_kind_values(self): self.bad(self.cfg(kind="mushy"))
    def test_never_soft_cannot_shrink(self):
        c = self.cfg(); c["ci_pipeline"]["soft_gate"]["never_soft"] = ["tests"]; self.bad(c)
        c = self.cfg(); c["ci_pipeline"]["soft_gate"]["never_soft"] = list(C.NEVER_SOFT) + ["extra"]; self.ok(c)
    def test_soft_gate_section_values(self):
        for k, v in (("return_soft_to_coder_until_iteration", -1), ("max_soft_findings_returned", "5"), ("result_status", "GREEN"), ("debt_created_by", "agent"), ("enabled", "yes")):
            c = self.cfg(); c["ci_pipeline"]["soft_gate"][k] = v; self.bad(c)
        for k, v in (("max_open_items", 0), ("register", "../x"), ("on_full", "ignore"), ("origin", "agent"), ("dedupe_by", ["rule"])):
            c = self.cfg(); c["ci_pipeline"]["soft_gate"]["debt_intake"][k] = v; self.bad(c)
    def test_autofix_section_values(self):
        for k, v in (("enabled", 1), ("max_touched_lines_outside_diff", 0), ("only_files_in_diff", False), ("counts_as_iteration", True)):
            c = self.cfg(); c["ci_pipeline"]["autofix"] = {k: v}; self.bad(c)
        c = self.cfg(); c["ci_pipeline"]["autofix"] = {"enabled": True, "only_files_in_diff": True, "counts_as_iteration": False}; self.ok(c)


class DriveSoft(DriveBase):
    """A teljes drive hurok autofixszel és puha kapuval. A sandbox-válaszsor CSAK a kapuhívásokat adja (syntax, unit_tests, lint_soft)."""
    def setUp(self):
        super().setUp()
        ci = self.cfg["ci_pipeline"]; ci["gates"].append(copy.deepcopy(SOFT_GATE)); ci["soft_gate"] = copy.deepcopy(SOFT_SECTION)
        ci["autofix"] = {"enabled": True, "max_touched_lines_outside_diff": 30}
        self.reg = DebtRegister(os.path.join(self.d, "state", "tech_debt.jsonl"), 30, self.audit)

    def go3(self, script, replies, transform=None, debt="reg", **kw):
        coder = self.prepare(script, replies)
        self.sb = AfSandbox(replies, transform=transform); self.gates.sandbox = self.sb
        self.afx = Autofixer(self.cfg, self.sb, self.tools, self.audit, self.root)
        return self.sup.drive("T-1", coder, self.gates, files=("src/coupon.py",), autofix=self.afx, debt=self.reg if debt == "reg" else debt, **kw)

    def it(self): return self.state.get_task("T-1")["iteration"]
    SOFT1 = [(0, ""), (0, ""), (1, E501(1))]          # syntax ok, unit ok, 1 puha találat


class TestAutofixInDrive(DriveSoft):
    def test_autofix_runs_before_gates_and_is_not_an_iteration(self):
        r = self.go3([GOOD_PATCH()], [(0, ""), (0, ""), (0, "")], transform=dq)
        self.assertEqual(r, "GREEN"); self.assertEqual(self.it(), 0); self.assertEqual(sorted(self.sb.af_calls[0]["argv"][3:]), ["src/coupon.py", "tests/unit/test_coupon.py"])
        self.assertNotIn("'", self.r("src/coupon.py")); self.assertNotIn("'", self.r("tests/unit/test_coupon.py")); self.assertIn("AUTOFIX_APPLIED", self.events())

    def test_gates_see_the_fixed_tree(self):
        self.go3([GOOD_PATCH()], [(0, ""), (0, ""), (0, "")], transform=dq)
        self.assertEqual(len(self.sb.calls), 3)            # az autofix-hívás nem a válaszsorból fogyaszt

    def test_created_file_is_fully_inside_the_diff(self):
        big = "".join(f"v{i} = 'x'\n" for i in range(40))
        r = self.go3([out([edit("src/coupon.py", "def apply_coupon(total, code):\n    return total\n", IMPL)], [{"path": "src/big.py", "content": big}])],
                     [(0, ""), (0, ""), (0, "")], transform=dq)
        self.assertEqual(r, "GREEN"); self.assertNotIn("'", self.r("src/big.py"))

    def test_files_created_in_an_earlier_round_stay_inside_the_diff(self):
        big = "".join(f"v{i} = 'x'\n" for i in range(40)); calls = []
        def late(rel, t):                                   # az 1. körben nem javít, a 2.-ban igen
            calls.append(rel); return dq(rel, t) if len([c for c in calls if c == "src/big.py"]) >= 2 else t
        r = self.go3([out([edit("src/coupon.py", "def apply_coupon(total, code):\n    return total\n", IMPL)], [{"path": "src/big.py", "content": big}]),
                      out([edit("src/big.py", "v0 = 'x'\n", "v0 = 'y'\n")])], [(0, ""), FAIL_A, (0, ""), (0, ""), (0, "")], transform=late)
        self.assertEqual(r, "GREEN"); self.assertNotIn("'", self.r("src/big.py"))

    def test_autofix_sandbox_refused_goes_to_human(self):
        coder = self.prepare([GOOD_PATCH()], [])
        self.sb = AfSandbox(raw=SandboxRefused("nincs")); self.gates.sandbox = self.sb
        afx = Autofixer(self.cfg, self.sb, self.tools, self.audit, self.root)
        self.assertEqual(self.sup.drive("T-1", coder, self.gates, files=("src/coupon.py",), autofix=afx, debt=self.reg), "ESCALATED")
        self.assertEqual(self.r("src/coupon.py"), IMPL)

    def test_without_autofix_nothing_changes(self):
        self.assertEqual(self.go([GOOD_PATCH()], GREEN + [(0, "")]), "GREEN")


class TestSoftGateInDrive(DriveSoft):
    def test_first_round_returns_few_soft_findings_to_coder(self):
        r = self.go3([GOOD_PATCH(), out([edit("src/coupon.py", IMPL, IMPL + "\n")])], self.SOFT1 + GREEN + [(0, "")])
        self.assertEqual(r, "GREEN"); self.assertEqual(self.it(), 1); self.assertIn("E501", self.ptext(1)); self.assertEqual(self.reg.open_count(), 0)
        self.assertEqual(self.state.get_task("T-1")["status"], "GATES_GREEN"); self.assertIn("SOFT_RETURNED", self.events())

    def test_second_round_soft_finding_becomes_debt(self):
        r = self.go3([GOOD_PATCH(), out([edit("src/coupon.py", IMPL, IMPL + "\n")])], self.SOFT1 + [(0, ""), (0, ""), (1, E501(1))])
        self.assertEqual(r, "GREEN_WITH_DEBT"); self.assertEqual(self.it(), 1); self.assertEqual(self.state.get_task("T-1")["status"], "GATES_GREEN_WITH_DEBT")
        it = self.reg.open_items(); self.assertEqual((len(it), it[0]["origin"], it[0]["task_id"]), (1, "soft_gate", "T-1")); self.assertEqual(self.gw.stats["calls"], 2)

    def test_more_than_max_soft_findings_go_straight_to_debt(self):
        r = self.go3([GOOD_PATCH()], [(0, ""), (0, ""), (1, E501(6))])
        self.assertEqual(r, "GREEN_WITH_DEBT"); self.assertEqual(self.it(), 0); self.assertEqual(self.gw.stats["calls"], 1); self.assertEqual(self.reg.open_count(), 1)   # (rule, file) szerint egy tétel

    def test_return_until_zero_never_returns(self):
        self.cfg["ci_pipeline"]["soft_gate"]["return_soft_to_coder_until_iteration"] = 0
        self.assertEqual(self.go3([GOOD_PATCH()], self.SOFT1), "GREEN_WITH_DEBT"); self.assertEqual(self.gw.stats["calls"], 1)

    def test_duplicate_debt_not_added_twice_but_still_green_with_debt(self):
        self.reg.add("T-0", [{"rule": "E501", "file": "src/coupon.py", "line": 1, "message": "x"}])
        self.assertEqual(self.go3([GOOD_PATCH()], [(0, ""), (0, ""), (1, E501(6))]), "GREEN_WITH_DEBT"); self.assertEqual(self.reg.open_count(), 1)

    def test_full_register_makes_soft_findings_hard(self):
        reg = DebtRegister(os.path.join(self.d, "state", "full.jsonl"), 1, self.audit); reg.add("T-0", [{"rule": "W291", "file": "z.py", "line": 1, "message": ""}])
        self.reg = reg; self.cfg["ci_pipeline"]["soft_gate"]["return_soft_to_coder_until_iteration"] = 0
        r = self.go3([GOOD_PATCH(), out([edit("src/coupon.py", IMPL, IMPL + "\n")])], [(0, ""), (0, ""), (1, E501(1))] + GREEN + [(0, "")])
        self.assertEqual(r, "GREEN"); self.assertEqual(self.it(), 1); self.assertIn("zárva", self.ptext(1)); self.assertIn("SOFT_GATE_CLOSED", self.events()); self.assertEqual(reg.open_count(), 1)

    def test_no_register_is_fail_closed(self):
        self.cfg["ci_pipeline"]["soft_gate"]["return_soft_to_coder_until_iteration"] = 0
        r = self.go3([GOOD_PATCH(), out([edit("src/coupon.py", IMPL, IMPL + "\n")])], [(0, ""), (0, ""), (1, E501(1))] + GREEN + [(0, "")], debt=None)
        self.assertEqual(r, "GREEN"); self.assertIn("zárva", self.ptext(1)); self.assertEqual(self.reg.open_count(), 0)

    def test_hard_failure_records_no_debt(self):
        self.go3([GOOD_PATCH()], [(1, "SyntaxError: x\n")]); self.assertEqual(self.reg.open_count(), 0)

    def test_soft_gate_counts_toward_iteration_cap_when_returned(self):
        self.cfg["iteration_policy"]["caps"]["S2"]["iterations"] = 1
        self.assertEqual(self.go3([GOOD_PATCH()], self.SOFT1), "CAP")


if __name__ == "__main__":
    unittest.main()
