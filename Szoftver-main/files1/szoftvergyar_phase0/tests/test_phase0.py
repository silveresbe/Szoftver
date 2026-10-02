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
from factory.qa import QA

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


if __name__ == "__main__":
    unittest.main()
