"""Patch-alkalmazó (12.2/4, 4.5): keresés-csere blokkok, védett zóna, atomi alkalmazás – 0 token, hálózat nélkül."""
import json, os, tempfile, unittest
from unittest import mock
from factory import patch as P
from factory.audit import AuditLog
from factory.killswitch import KillSwitch, Killed
from factory.tool_gateway import ToolGateway


def edit(path, search, replace):
    return {"path": path, "search": search, "replace": replace}


def new(path, content):
    return {"path": path, "content": content}


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.d = self._t.name
        self.root = os.path.join(self.d, "repo")
        self.audit = AuditLog(os.path.join(self.d, "audit", "a.jsonl"))
        self.kill = KillSwitch(os.path.join(self.d, "state"))
        self.tools = ToolGateway({}, self.audit, self.kill)
        os.makedirs(os.path.join(self.root, "src"))
        os.makedirs(os.path.join(self.root, "tests", "acceptance"))
        self.w("src/app.py", "def a():\n    return 1\n\n\ndef b():\n    return 2\n")
        self.w("tests/acceptance/test_ac.py", "def test_ac():\n    assert True\n")

    def tearDown(self):
        self._t.cleanup()

    def w(self, rel, text, mode="w"):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, mode, **({} if "b" in mode else {"encoding": "utf-8", "newline": ""})) as f:
            f.write(text)

    def r(self, rel):
        with open(os.path.join(self.root, rel), "rb") as f:
            return f.read().decode("utf-8")

    def ap(self, blocks, **kw):
        return P.apply_patch(blocks, self.root, self.tools, **kw)

    def codes(self, res):
        return [e["code"] for e in res["errors"]]

    def audit_events(self):
        if not os.path.exists(self.audit.path):
            return []
        with open(self.audit.path, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]


class TestEdit(Base):
    def test_simple_edit(self):
        res = self.ap([edit("src/app.py", "return 1", "return 10")])
        self.assertTrue(res["ok"]); self.assertEqual(res["applied"], ["src/app.py"])
        self.assertIn("return 10", self.r("src/app.py")); self.assertIn("return 2", self.r("src/app.py"))
        self.assertEqual(res["stats"], {"files": 1, "added": 1, "removed": 1})

    def test_multiline_search(self):
        res = self.ap([edit("src/app.py", "def a():\n    return 1\n", "def a():\n    return 3\n")])
        self.assertTrue(res["ok"]); self.assertIn("return 3", self.r("src/app.py"))

    def test_search_not_found_is_retryable_with_context(self):
        before = self.r("src/app.py")
        res = self.ap([edit("src/app.py", "def a():\n    return 99\n", "x")])
        self.assertFalse(res["ok"]); self.assertEqual(self.codes(res), ["SEARCH_NOT_FOUND"]); self.assertTrue(res["retryable"])
        self.assertIn("return 1", res["errors"][0]["context"])           # a valódi környezet, sorszámmal
        self.assertRegex(res["errors"][0]["context"], r"^\d+: ")
        self.assertEqual(self.r("src/app.py"), before)

    def test_ambiguous_search_rejected(self):
        self.w("src/dup.py", "x = 1\nx = 1\n")
        res = self.ap([edit("src/dup.py", "x = 1", "x = 2")])
        self.assertEqual(self.codes(res), ["SEARCH_AMBIGUOUS"]); self.assertTrue(res["retryable"])
        self.assertIn("1, 2", res["errors"][0]["context"]); self.assertEqual(self.r("src/dup.py"), "x = 1\nx = 1\n")

    def test_overlapping_matches_are_ambiguous(self):
        self.w("src/o.py", "aaa")
        self.assertEqual(self.codes(self.ap([edit("src/o.py", "aa", "b")])), ["SEARCH_AMBIGUOUS"])

    def test_no_context_when_nothing_similar(self):
        res = self.ap([edit("src/app.py", "QQQQQQQQQQQQ zzzzzzzz", "x")])
        self.assertNotIn("context", res["errors"][0])

    def test_empty_search_and_noop_rejected(self):
        self.assertEqual(self.codes(self.ap([edit("src/app.py", "", "x")])), ["BAD_BLOCK"])
        self.assertEqual(self.codes(self.ap([edit("src/app.py", "return 1", "return 1")])), ["BAD_BLOCK"])

    def test_edit_of_missing_file(self):
        res = self.ap([edit("src/nincs.py", "a", "b")])
        self.assertEqual(self.codes(res), ["NOT_FOUND"]); self.assertFalse(res["retryable"])

    def test_sequential_edits_same_file_see_previous_result(self):
        res = self.ap([edit("src/app.py", "return 1", "return 10"), edit("src/app.py", "return 10", "return 11")])
        self.assertTrue(res["ok"]); self.assertIn("return 11", self.r("src/app.py"))

    def test_failure_stops_later_blocks_of_same_file_only(self):
        self.w("src/b.py", "y = 1\n")
        res = self.ap([edit("src/app.py", "NINCS ILYEN SOR", "x"), edit("src/app.py", "return 2", "return 20"),
                       edit("src/b.py", "y = 1", "y = 2")])
        self.assertEqual([e["block"] for e in res["errors"]], [0])          # a 2. blokk (ugyanaz a fájl) nem ad félrevezető hibát
        self.assertEqual(self.r("src/b.py"), "y = 1\n")                     # semmi nem íródott (mindent vagy semmit)

    def test_crlf_file_preserved(self):
        self.w("src/w.txt", "a\r\nb\r\nc\r\n")
        res = self.ap([edit("src/w.txt", "b\nc\n", "B\nC\n")])
        self.assertTrue(res["ok"]); self.assertEqual(self.r("src/w.txt"), "a\r\nB\r\nC\r\n")

    def test_file_mode_preserved(self):
        p = os.path.join(self.root, "src", "run.sh"); self.w("src/run.sh", "echo a\n"); os.chmod(p, 0o755)
        self.assertTrue(self.ap([edit("src/run.sh", "echo a", "echo b")])["ok"])
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o755)

    def test_binary_and_non_utf8_rejected(self):
        self.w("src/bin.dat", b"\xff\xfe\x00\x01", mode="wb")
        self.assertEqual(self.codes(self.ap([edit("src/bin.dat", "a", "b")])), ["NOT_TEXT"])

    def test_too_large(self):
        self.assertEqual(self.codes(self.ap([edit("src/app.py", "return 1", "x" * 200)], limits={"max_file_bytes": 100})), ["TOO_LARGE"])
        self.assertEqual(self.codes(self.ap([new("src/n.py", "x" * 200)], limits={"max_file_bytes": 100})), ["TOO_LARGE"])


class TestNewFile(Base):
    def test_create_with_dirs(self):
        res = self.ap([new("src/pkg/mod.py", "X = 1\n")])
        self.assertTrue(res["ok"]); self.assertEqual(self.r("src/pkg/mod.py"), "X = 1\n")
        self.assertEqual(os.stat(os.path.join(self.root, "src/pkg/mod.py")).st_mode & 0o777, 0o644)

    def test_create_existing_rejected(self):
        before = self.r("src/app.py")
        res = self.ap([new("src/app.py", "x")])
        self.assertEqual(self.codes(res), ["EXISTS"]); self.assertFalse(res["retryable"]); self.assertEqual(self.r("src/app.py"), before)

    def test_create_then_edit_in_same_patch(self):
        res = self.ap([new("src/n.py", "A = 1\n"), edit("src/n.py", "A = 1", "A = 2")])
        self.assertTrue(res["ok"]); self.assertEqual(self.r("src/n.py"), "A = 2\n")

    def test_create_twice_in_same_patch_rejected(self):
        self.assertEqual(self.codes(self.ap([new("src/n.py", "1"), new("src/n.py", "2")])), ["EXISTS"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "src", "n.py")))

    def test_empty_content_allowed(self):
        self.assertTrue(self.ap([new("src/__init__.py", "")])["ok"]); self.assertEqual(self.r("src/__init__.py"), "")


class TestShapeAndLimits(Base):
    def test_bad_shapes(self):
        for b in ("szöveg", {}, {"path": "src/app.py"}, {"path": "a", "search": "x"}, {"path": "a", "content": "x", "search": "y"},
                  {"path": "src/app.py", "search": 1, "replace": "x"}, {"path": "src/app.py", "content": None}):
            self.assertEqual(self.codes(self.ap([b])), ["BAD_BLOCK"], b)

    def test_empty_or_non_list_patch(self):
        self.assertEqual(self.codes(self.ap([])), ["BAD_BLOCK"]); self.assertEqual(self.codes(self.ap(None)), ["BAD_BLOCK"])

    def test_too_many_blocks(self):
        self.assertEqual(self.codes(self.ap([edit("src/app.py", "return 1", "return 3")] * 3, limits={"max_blocks": 2})), ["BAD_BLOCK"])

    def test_all_errors_reported_in_one_go(self):
        res = self.ap([edit("src/app.py", "NINCS", "x"), edit("src/nincs.py", "a", "b"), new("src/app.py", "x")])
        self.assertEqual(sorted(self.codes(res)), ["NOT_FOUND", "SEARCH_NOT_FOUND"])
        self.assertEqual(len(res["errors"]), 2)                              # a 3. blokk ugyanaz a fájl, mint az 1.

    def test_retryable_only_if_all_errors_retryable(self):
        res = self.ap([edit("src/app.py", "NINCS", "x"), edit("src/nincs.py", "a", "b")])
        self.assertFalse(res["retryable"])

    def test_nonexistent_root(self):
        with self.assertRaises(ValueError):
            P.apply_patch([new("a.py", "x")], os.path.join(self.d, "nincs"), self.tools)


class TestProtectedZone(Base):
    """A csak olvasható tests/acceptance/ és a védett zóna: közvetlenül és trükkös útvonalakkal is zárt (4.5)."""

    def test_acceptance_edit_denied_and_untouched(self):
        before = self.r("tests/acceptance/test_ac.py")
        res = self.ap([edit("tests/acceptance/test_ac.py", "assert True", "assert False")])
        self.assertEqual(self.codes(res), ["DENIED"]); self.assertFalse(res["retryable"])
        self.assertEqual(self.r("tests/acceptance/test_ac.py"), before)

    def test_acceptance_new_file_denied(self):
        res = self.ap([new("tests/acceptance/test_new.py", "x")])
        self.assertEqual(self.codes(res), ["DENIED"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "tests", "acceptance", "test_new.py")))

    def test_protected_block_blocks_whole_patch(self):
        before = self.r("src/app.py")
        res = self.ap([edit("src/app.py", "return 1", "return 10"), new("tests/acceptance/x.py", "x")])
        self.assertFalse(res["ok"]); self.assertEqual(self.r("src/app.py"), before)      # mindent vagy semmit

    def test_tricky_paths_denied(self):
        for p in ("tests/x/../acceptance/test_ac.py", "./tests/acceptance/test_ac.py", "tests//acceptance/test_ac.py",
                  "Tests/Acceptance/test_ac.py", "TESTS/ACCEPTANCE/new.py", "src/../tests/acceptance/n.py"):
            self.assertEqual(self.codes(self.ap([new(p, "x")])), ["DENIED"], p)
            self.assertEqual(self.codes(self.ap([edit(p, "assert True", "assert False")])), ["DENIED"], p)
        self.assertIn("assert True", self.r("tests/acceptance/test_ac.py"))

    def test_other_protected_paths_denied(self):
        for p in ("core/prompts/x.md", "factory/patch.py", "factory.yaml", "state/KILL", "audit/a.jsonl", "secrets/k", ".env", ".env.local",
                  "modules/m.md", "evals/baseline.json", "evals/recordings/r.json"):
            self.assertEqual(self.codes(self.ap([new(p, "x")])), ["DENIED"], p)

    def test_git_dir_denied(self):
        for p in (".git/config", ".GIT/hooks/pre-commit", "sub/.git/config"):
            self.assertEqual(self.codes(self.ap([new(p, "x")])), ["DENIED"], p)

    def test_bad_paths(self):
        for p in ("/etc/passwd", "../kint.py", "src/../../kint.py", "..", ".", "src\\app.py", "C:/x.py", "", "  ", "src/", "a\x00b.py"):
            self.assertEqual(self.codes(self.ap([new(p, "x")])), ["BAD_PATH"], repr(p))
        self.assertFalse(os.path.exists(os.path.join(self.d, "kint.py")))

    def test_symlink_file_and_dir_rejected(self):
        outside = os.path.join(self.d, "kint.txt"); open(outside, "w").write("titok")
        os.symlink(outside, os.path.join(self.root, "src", "link.txt"))
        os.symlink(os.path.join(self.root, "tests", "acceptance"), os.path.join(self.root, "src", "acclink"))
        os.symlink(self.d, os.path.join(self.root, "src", "outdir"))
        for res in (self.ap([edit("src/link.txt", "titok", "x")]), self.ap([new("src/acclink/n.py", "x")]),
                    self.ap([edit("src/acclink/test_ac.py", "assert True", "assert False")]), self.ap([new("src/outdir/n.py", "x")])):
            self.assertEqual(self.codes(res), ["BAD_PATH"])
        self.assertEqual(open(outside).read(), "titok")
        self.assertIn("assert True", self.r("tests/acceptance/test_ac.py"))
        self.assertFalse(os.path.exists(os.path.join(self.d, "n.py")))

    def test_agent_without_repo_write_denied(self):
        for agent in ("product_owner", "qa", "security", "nincs_ilyen"):
            self.assertEqual(self.codes(self.ap([new("src/n.py", "x")], agent=agent)), ["DENIED"], agent)

    def test_custom_protected_paths_from_config(self):
        tools = ToolGateway({"tool_permissions": {"protected_paths": ["src/kesz/**"]}}, self.audit, self.kill)
        res = P.apply_patch([new("src/kesz/a.py", "x")], self.root, tools)
        self.assertEqual(self.codes(res), ["DENIED"])

    def test_tools_is_required(self):
        with self.assertRaises(TypeError):
            P.apply_patch([new("src/n.py", "x")], self.root)


class TestAtomicity(Base):
    def test_write_failure_rolls_back_all(self):
        self.w("src/b.py", "y = 1\n")
        orig_replace, calls = os.replace, {"n": 0}

        def flaky(a, b):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("lemez tele")
            return orig_replace(a, b)

        b_before, app_before = self.r("src/b.py"), self.r("src/app.py")
        with mock.patch("factory.patch.os.replace", flaky):
            with self.assertRaises(OSError):
                self.ap([edit("src/app.py", "return 1", "return 10"), edit("src/b.py", "y = 1", "y = 2"), new("src/deep/n/x.py", "z")])
        self.assertEqual(self.r("src/app.py"), app_before); self.assertEqual(self.r("src/b.py"), b_before)
        self.assertFalse(os.path.exists(os.path.join(self.root, "src", "deep")))
        self.assertEqual([f for f in os.listdir(os.path.join(self.root, "src")) if f.startswith(".patch-")], [])   # nincs maradék tmp
        self.assertIn("PATCH_FAILED_ROLLED_BACK", [e["event"] for e in self.audit_events()])

    def test_rollback_restores_created_file_removal(self):
        orig_replace, calls = os.replace, {"n": 0}

        def flaky(a, b):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("x")
            return orig_replace(a, b)

        with mock.patch("factory.patch.os.replace", flaky):
            with self.assertRaises(OSError):
                self.ap([new("src/first.py", "1"), new("src/second.py", "2")])
        self.assertFalse(os.path.exists(os.path.join(self.root, "src", "first.py")))

    def test_dry_run_writes_nothing_and_no_applied_audit(self):
        res = self.ap([edit("src/app.py", "return 1", "return 10"), new("src/n.py", "x")], dry_run=True)
        self.assertTrue(res["ok"]); self.assertTrue(res["dry_run"])
        self.assertIn("return 1\n", self.r("src/app.py")); self.assertFalse(os.path.exists(os.path.join(self.root, "src", "n.py")))
        self.assertNotIn("PATCH_APPLIED", [e["event"] for e in self.audit_events()])

    def test_dry_run_of_protected_still_denied(self):
        self.assertEqual(self.codes(self.ap([new("tests/acceptance/n.py", "x")], dry_run=True)), ["DENIED"])

    def test_kill_switch_before_write(self):
        self.kill.trip("teszt")
        before = self.r("src/app.py")
        with self.assertRaises(Killed):
            self.ap([edit("src/app.py", "return 1", "return 10")])
        self.assertEqual(self.r("src/app.py"), before)

    def test_kill_switch_tripped_between_validation_and_write(self):
        """A validálás után, közvetlenül az írás előtt is megáll a kill switchre (6.9): a korai authorize-ellenőrzés nem elég."""
        real_stats = P._stats

        def trip_then_stats(plan):
            self.kill.trip("versenyhelyzet")
            return real_stats(plan)

        before = self.r("src/app.py")
        with mock.patch("factory.patch._stats", trip_then_stats):
            with self.assertRaises(Killed):
                self.ap([edit("src/app.py", "return 1", "return 10")])
        self.assertEqual(self.r("src/app.py"), before)

    def test_noop_result_after_edit_and_revert(self):
        res = self.ap([edit("src/app.py", "return 1", "return 10"), edit("src/app.py", "return 10", "return 1")])
        self.assertTrue(res["ok"]); self.assertEqual(res["applied"], [])


class TestAudit(Base):
    def test_applied_logged_without_content_and_chain_valid(self):
        self.ap([edit("src/app.py", "return 1", "return TITKOS_TARTALOM"), new("src/n.py", "MASIK_TITOK")])
        ev = [e for e in self.audit_events() if e["event"] == "PATCH_APPLIED"]
        self.assertEqual(len(ev), 1); self.assertEqual(sorted(ev[0]["detail"]["files"]), ["src/app.py", "src/n.py"])
        raw = open(self.audit.path, encoding="utf-8").read()
        self.assertNotIn("TITKOS_TARTALOM", raw); self.assertNotIn("MASIK_TITOK", raw)
        self.audit.verify()

    def test_rejection_logged_without_content(self):
        self.ap([edit("src/app.py", "TITOK_KERESES", "TITOK_CSERE")])
        ev = [e for e in self.audit_events() if e["event"] == "PATCH_REJECTED"]
        self.assertEqual(ev[0]["detail"]["errors"][0]["code"], "SEARCH_NOT_FOUND")
        raw = open(self.audit.path, encoding="utf-8").read()
        self.assertNotIn("TITOK_KERESES", raw); self.assertNotIn("TITOK_CSERE", raw)

    def test_repo_content_in_context_never_reaches_audit(self):
        """A `context` a repó sorait tartalmazza (akár titkot): a válaszban ott van, az auditban SOHA."""
        self.w("src/cfg.py", "SECRET_MARKER_LINE = 1\nmasik = 2\n")
        res = self.ap([edit("src/cfg.py", "SECRET_MARKER_LINE = 2", "x")])
        self.assertIn("SECRET_MARKER_LINE", res["errors"][0]["context"])
        raw = open(self.audit.path, encoding="utf-8").read()
        self.assertNotIn("SECRET_MARKER_LINE", raw); self.assertNotIn("context", raw)
        self.assertNotIn("a 'search' szöveg nem található", raw)               # a részlet sem kerül be, csak blokk+út+kód

    def test_denied_is_audited_by_tool_gateway(self):
        self.ap([new("tests/acceptance/n.py", "x")])
        self.assertIn("TOOL_DENIED", [e["event"] for e in self.audit_events()])
        self.ap([new("Tests/Acceptance/n.py", "x")])
        self.assertEqual([e["detail"].get("via") for e in self.audit_events() if e["event"] == "TOOL_DENIED"][-1], "casefold")


class TestReadFile(Base):
    """Az olvasás ugyanazokat az útvonalszabályokat követi, mint az írás."""

    def test_reads_text(self):
        self.assertEqual(P.read_file(self.root, "src/app.py"), (self.r("src/app.py"), None))

    def test_errors(self):
        os.symlink(self.d, os.path.join(self.root, "src", "outdir"))
        self.w("src/bin.dat", b"\xff\x00", mode="wb")
        cases = {"nincs.py": "NOT_FOUND", "../x": "BAD_PATH", "/etc/passwd": "BAD_PATH", "src/outdir/x": "BAD_PATH", "src/bin.dat": "NOT_TEXT",
                 ".git/config": "DENIED", "src": "BAD_PATH", "": "BAD_PATH"}
        for rel, code in cases.items():
            text, err = P.read_file(self.root, rel)
            self.assertIsNone(text, rel); self.assertEqual(err[0], code, rel)

    def test_too_large(self):
        self.assertEqual(P.read_file(self.root, "src/app.py", limits={"max_file_bytes": 5})[1][0], "TOO_LARGE")


class TestRetryText(Base):
    def test_retry_text_terse_with_context(self):
        res = self.ap([edit("src/app.py", "def a():\n    return 99\n", "x")])
        t = P.retry_text(res["errors"])
        self.assertTrue(t.startswith("#0 src/app.py SEARCH_NOT_FOUND:")); self.assertIn("return 1", t)
        self.assertNotIn("return 99", t)                                     # a Coder hibás keresése nem megy vissza

    def test_context_is_bounded(self):
        self.w("src/big.py", "\n".join(f"valtozo_{i} = {i}" for i in range(500)))
        c = P.hunk_context(self.r("src/big.py"), "valtozo_250 = 999", radius=5, max_chars=200)
        self.assertLessEqual(len(c), 201); self.assertIn("valtozo_250 = 250", c)        # a legjobb találat marad, nem a vége vágódik


if __name__ == "__main__":
    unittest.main()
