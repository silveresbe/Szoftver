import copy, json, os, tempfile, unittest, yaml
from factory import config as C, messages as M
from factory.audit import AuditLog, AuditChainBroken
from factory.state import State
from factory.rate_limiter import RateLimiter, RequestTooLarge, WaitTooLong, DailyQuotaExhausted
from factory.redactor import Redactor, RedactorError, ForbiddenPath
from factory.gateway import Gateway, ProviderUnavailable, TruncatedOutput, OutputContractFailed, ReplayMiss
from factory.tool_gateway import ToolGateway, ToolDenied
from factory.killswitch import KillSwitch, Killed
from factory.sandbox import SandboxRunner, SandboxRefused
from factory.supervisor import Supervisor, PhaseNotEnabled
from factory import ingress
from factory.lane_classifier import LaneClassifier

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, "factory.yaml")) as _f: CFG = yaml.safe_load(_f)

class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t
    def sleep(self, s): self.t += s

class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cfg = copy.deepcopy(CFG)
        self.audit = AuditLog(os.path.join(self.d, "audit", "d.jsonl"))
        self.state = State(os.path.join(self.d, "state", "f.db"))
        self.kill = KillSwitch(os.path.join(self.d, "state"))
    def tearDown(self): self.state.close()

class TestLaneClassifier(unittest.TestCase):
    def test_easy_doc_fix_is_s1(self):
        cl = LaneClassifier()
        self.assertEqual(cl.classify("Fix typo in README and update docs"), "S1")
        self.assertEqual(cl.classify({"text": "Fix typo in README", "size": "small", "kind": "docs"}), "S1")

    def test_security_and_refactor_are_s2(self):
        cl = LaneClassifier()
        self.assertEqual(cl.classify("Add OAuth login flow with auth checks"), "S2")
        self.assertEqual(cl.classify({"text": "Refactor auth module", "files": ["auth.py", "token.py", "session.py"]}), "S2")

    def test_explicit_lane_is_respected(self):
        cl = LaneClassifier()
        self.assertEqual(cl.classify({"text": "tiny doc cleanup", "lane": "S2"}), "S2")
        self.assertEqual(cl.classify({"text": "security cleanup", "lane": "S1"}), "S1")


class TestConfig(unittest.TestCase):
    def test_sample_valid(self): C.validate(copy.deepcopy(CFG))
    def test_network_must_be_none(self):
        c = copy.deepcopy(CFG); c["sandbox"]["network"] = "bridge"
        with self.assertRaises(C.ConfigError): C.validate(c)
    def test_missing_section(self):
        c = copy.deepcopy(CFG); del c["rate_limit"]
        with self.assertRaises(C.ConfigError): C.validate(c)
    def test_live_needs_ack(self):
        c = copy.deepcopy(CFG); c["llm_gateway_modes"]["mode"] = "live"
        with self.assertRaises(C.ConfigError): C.validate(c)
    def test_host_mounts_forbidden(self):
        c = copy.deepcopy(CFG); c["sandbox"]["host_mounts"] = ["/etc"]
        with self.assertRaises(C.ConfigError): C.validate(c)

class TestAudit(Base):
    def test_chain_ok_and_tamper(self):
        for i in range(5): self.audit.append("x", "E", {"i": i})
        self.assertEqual(self.audit.verify(), 5)
        with open(self.audit.path) as f: lines = f.read().splitlines()
        e = json.loads(lines[2]); e["detail"]["i"] = 99; lines[2] = json.dumps(e, sort_keys=True)
        with open(self.audit.path, "w") as f: f.write("\n".join(lines) + "\n")
        with self.assertRaises(AuditChainBroken): AuditLog(self.audit.path).verify()
    def test_reopen_keeps_head(self):
        self.audit.append("x", "E"); h = self.audit.head
        self.assertEqual(AuditLog(self.audit.path).head, h)

class TestMessages(unittest.TestCase):
    def test_bad_type(self):
        with self.assertRaises(M.MessageError): M.make("T-1", "qa", "supervisor", "NOPE")
    def test_task_assign_needs_lane(self):
        with self.assertRaises(M.MessageError): M.make("T-1", "supervisor", "master_coder", "TASK_ASSIGN", {})
    def test_schema(self):
        s = {"type": "object", "required": ["verdict"], "properties": {"verdict": {"type": "string", "enum": ["APPROVE", "REJECT"]}, "reason": {"type": "string", "maxLength": 5}}}
        self.assertEqual(M.schema_errors({"verdict": "APPROVE"}, s), [])
        self.assertTrue(M.schema_errors({"verdict": "X", "reason": "toolong"}, s))

class TestRateLimiter(Base):
    def rl(self):
        clk = Clock(); return RateLimiter(self.cfg, self.state, clk), clk
    def test_wait_then_go(self):
        rl, clk = self.rl(); m = "llama-3.1-8b-instant"   # tpm 6000*0.9=5400, share .5 => 2700/kérés
        h1, w1 = rl.try_reserve(m, 2700); h2, w2 = rl.try_reserve(m, 2700); h3, w3 = rl.try_reserve(m, 2700)
        self.assertTrue(h1 and h2); self.assertIsNone(h3); self.assertGreater(w3, 0)
        clk.t += w3 + 1; self.assertTrue(rl.try_reserve(m, 2700)[0])
    def test_too_large(self):
        rl, _ = self.rl()
        with self.assertRaises(RequestTooLarge): rl.try_reserve("llama-3.1-8b-instant", 3000)
    def test_settle_returns_surplus(self):
        rl, _ = self.rl(); h, _ = rl.try_reserve(m, 2700); rl.settle(h, 100)
        self.assertTrue(rl.try_reserve(m, 2700)[0]); self.assertTrue(rl.try_reserve(m, 2600)[0])
    def test_daily_quota_persistent(self):
        rl, _ = self.rl(); m = "openai/gpt-oss-120b"; self.state.add_daily(m, 179_000, 1, rl.clock())
        with self.assertRaises(DailyQuotaExhausted): rl.try_reserve(m, 2000)
    def test_429_penalty(self):
        rl, clk = self.rl(); m = "llama-3.1-8b-instant"; rl.on_provider_429(m, 20)
        h, w = rl.try_reserve(m, 100); self.assertIsNone(h); self.assertAlmostEqual(w, 20, delta=1)
    def test_wait_too_long(self):
        self.cfg["rate_limit"]["max_wait_seconds"] = 5; clk = Clock(); rl = RateLimiter(self.cfg, self.state, clk)
        m = "llama-3.1-8b-instant"; rl.reserve(m, 2700, clk.sleep); rl.reserve(m, 2700, clk.sleep)
        with self.assertRaises(WaitTooLong): rl.reserve(m, 2700, clk.sleep)
    def test_aging_priority(self):
        rl, _ = self.rl()
        self.assertLess(rl.effective_priority("supervisor", 0), rl.effective_priority("tech_writer", 0))
        self.assertLess(rl.effective_priority("tech_writer", 1200), rl.effective_priority("tech_writer", 0))

class TestRedactor(unittest.TestCase):
    def test_masks(self):
        t, n = Redactor(CFG).redact("k=gsk_abcdefghijklmnop1234 mail a@b.hu")
        self.assertNotIn("gsk_", t); self.assertNotIn("a@b.hu", t); self.assertIn("[TITOK:", t)
    def test_private_key(self):
        t, _ = Redactor(CFG).redact("-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----")
        self.assertNotIn("abc", t)
    def test_fail_closed(self):
        r = Redactor(CFG); r._broken = True
        with self.assertRaises(RedactorError): r.redact("x")
    def test_forbidden_paths(self):
        with self.assertRaises(ForbiddenPath): Redactor(CFG).check_paths([".env.local"])
        with self.assertRaises(ForbiddenPath): Redactor(CFG).check_paths(["secrets/a.txt"])
        Redactor(CFG).check_paths(["src/app.py"])

class TestGateway(Base):
    SCHEMA = {"type": "object", "required": ["verdict"], "properties": {"verdict": {"type": "string", "enum": ["APPROVE", "REJECT"]}}}
    def gw(self, script=(), mode="mock"):
        self.cfg["llm_gateway_modes"]["mode"] = mode
        clk = Clock(); rl = RateLimiter(self.cfg, self.state, clk)
        return Gateway(self.cfg, rl, Redactor(self.cfg), self.audit, recordings_dir=os.path.join(self.d, "rec"), sleep=clk.sleep, mock_script=script)
    M = "llama-3.3-70b-versatile"; MSG = [{"role": "user", "content": "szia"}]
    def test_ok(self):
        r = self.gw(['{"verdict":"APPROVE"}']).call("qa", self.M, self.MSG, self.MSG, 100, self.SCHEMA); self.assertEqual(r["data"]["verdict"], "APPROVE")
    def test_single_repair_then_ok(self):
        g = self.gw([{"fault": "schema_violation"}, '{"verdict":"REJECT"}'])
        self.assertEqual(g.call("qa", self.M, self.MSG, self.MSG, 100, self.SCHEMA)["data"]["verdict"], "REJECT"); self.assertEqual(g.stats["repairs"], 1)
    def test_repair_fails_stops_thread(self):
        with self.assertRaises(OutputContractFailed): self.gw([{"fault": "schema_violation"}, {"fault": "schema_violation"}]).call("qa", self.M, self.MSG, self.MSG, 100, self.SCHEMA)
    def test_truncated_never_passed(self):
        with self.assertRaises(TruncatedOutput): self.gw([{"fault": "truncated"}]).call("qa", self.M, self.MSG, self.MSG, 100, self.SCHEMA)
    def test_429_then_ok(self):
        g = self.gw([{"fault": "429", "retry_after": 2}, '{"verdict":"APPROVE"}'])
        self.assertEqual(g.call("qa", self.M, self.MSG, self.MSG, 100, self.SCHEMA)["data"]["verdict"], "APPROVE")
    def test_provider_down(self):
        with self.assertRaises(ProviderUnavailable): self.gw([{"fault": "provider_down"}]).call("qa", self.M, self.MSG, self.MSG, 100)
    def test_timeout_retries_exhausted(self):
        with self.assertRaises(ProviderUnavailable): self.gw([{"fault": "timeout"}] * 5).call("qa", self.M, self.MSG, self.MSG, 100)
    def test_egress_fail_closed_no_call(self):
        g = self.gw(['{"verdict":"APPROVE"}']); g.redactor._broken = True
        with self.assertRaises(RedactorError): g.call("qa", self.M, self.MSG, self.MSG, 100)
        self.assertEqual(len(g.mock_script), 1)   # a hívás nem ment ki
    def test_secret_not_sent(self):
        seen = []; g = self.gw(); orig = g._mock
        g._mock = lambda m, msgs, mt: (seen.append(msgs), orig(m, msgs, mt))[1]
        g.call("qa", self.M, [{"role": "user", "content": "kulcs gsk_abcdefghijklmnop1234"}], 100)
        self.assertNotIn("gsk_", json.dumps(seen))
    def test_record_replay(self):
        g = self.gw(mode="record"); g.provider = lambda m, msgs, mt: {"text": "valasz gsk_abcdefghijklmnop1234", "finish_reason": "stop", "tokens": 12}
        g.call("qa", self.M, self.MSG, 100)
        r = self.gw(mode="replay").call("qa", self.M, self.MSG, 100)
        self.assertIn("valasz", r["data"]); self.assertNotIn("gsk_", r["data"])
    def test_replay_miss(self):
        with self.assertRaises(ReplayMiss): self.gw(mode="replay").call("qa", self.M, [{"role": "user", "content": "ismeretlen"}], 100)

# Keep the rest of the original test file unchanged below this point.
