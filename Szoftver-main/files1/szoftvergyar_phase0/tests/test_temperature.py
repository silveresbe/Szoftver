"""agents.<ágens>.temperature: konfig -> ágens -> Gateway -> szolgáltató (mock/replay módban, 0 token)."""
import copy, json, os, unittest
from factory import config as C
from factory.gateway import prompt_key
from tests.test_master_coder import CoderBase, GOOD_PATCH
from tests.test_product_owner import POBase, good, CFG


def spy(gw):
    """A Gateway._raw hívásait figyeli: (temperature kwarg vagy None)."""
    seen = []; raw = gw._raw
    gw._raw = lambda model, messages, mt, mh, **k: (seen.append(k.get("temperature")), raw(model, messages, mt, mh, **k))[1]
    return seen


class TestCoderTemperature(CoderBase):
    def test_coder_passes_configured_temperature(self):
        c = self.build([GOOD_PATCH()]); seen = spy(self.gw)
        from tests.test_master_coder import good as g
        c.run("T-1", g(), files=("src/coupon.py",))
        self.assertEqual(seen, [0.2])

    def test_repair_call_uses_same_temperature(self):
        c = self.build([{"fault": "schema_violation"}, GOOD_PATCH()]); seen = spy(self.gw)
        from tests.test_master_coder import good as g
        c.run("T-1", g(), files=("src/coupon.py",))
        self.assertEqual(seen, [0.2, 0.2])

    def test_no_temperature_configured_means_provider_default(self):
        del self.cfg["agents"]["master_coder"]["temperature"]
        c = self.build([GOOD_PATCH()]); seen = spy(self.gw)
        from tests.test_master_coder import good as g
        c.run("T-1", g(), files=("src/coupon.py",))
        self.assertEqual(seen, [None])


class TestPoTemperature(POBase):
    def test_po_passes_configured_temperature(self):
        po = self.build([json.dumps(good())]); seen = spy(self.gw); po.run("T-1", "kupon")
        self.assertEqual(seen, [0.3])


class TestProviderAndKey(unittest.TestCase):
    M = "m"; MSG = [{"role": "user", "content": "x"}]

    def test_key_changes_with_temperature(self):
        self.assertNotEqual(prompt_key(self.M, self.MSG, "h", 0.2), prompt_key(self.M, self.MSG, "h", 0.7))
        self.assertNotEqual(prompt_key(self.M, self.MSG, "h", 0.2), prompt_key(self.M, self.MSG, "h"))

    def test_key_without_temperature_is_backward_compatible(self):
        import hashlib
        old = hashlib.sha256(json.dumps([self.M, "h", self.MSG], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.assertEqual(prompt_key(self.M, self.MSG, "h"), old)

    def test_groq_request_body_contains_temperature(self):
        from factory.gateway import GroqProvider
        import urllib.request, io
        sent = {}
        class R(io.BytesIO):
            headers = {}
            def __enter__(s): return s
            def __exit__(s, *a): pass
        def fake(req, timeout=0):
            sent["b"] = json.loads(req.data); return R(json.dumps({"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}], "usage": {"total_tokens": 3}}).encode())
        orig, os.environ["GROQ_API_KEY"] = urllib.request.urlopen, "k"
        urllib.request.urlopen = fake
        try:
            GroqProvider()(self.M, self.MSG, 10, temperature=0.2); self.assertEqual(sent["b"]["temperature"], 0.2)
            GroqProvider()(self.M, self.MSG, 10); self.assertNotIn("temperature", sent["b"])
        finally:
            urllib.request.urlopen = orig; os.environ.pop("GROQ_API_KEY", None)


class TestGatewayToProvider(unittest.TestCase):
    """Record módban a Gateway.call temperature-je a szolgáltatóig ér; megadás nélkül a szolgáltató semmit nem kap."""
    def gw(self):
        from factory.gateway import Gateway
        from factory.rate_limiter import RateLimiter
        from factory.redactor import Redactor
        from factory.state import State
        from factory.audit import AuditLog
        from tests.test_product_owner import Clock
        import tempfile
        d = tempfile.mkdtemp(); cfg = copy.deepcopy(CFG); cfg["llm_gateway_modes"]["mode"] = "record"; clk = Clock()
        st = State(os.path.join(d, "s.db")); self.addCleanup(st.close)
        return Gateway(cfg, RateLimiter(cfg, st, clk), Redactor(cfg), AuditLog(os.path.join(d, "a.jsonl")), recordings_dir=os.path.join(d, "rec"), sleep=clk.sleep)

    M = "llama-3.3-70b-versatile"; MSG = [{"role": "user", "content": "szia"}]
    OK = {"text": "ok", "finish_reason": "stop", "tokens": 5}

    def test_temperature_reaches_provider(self):
        g = self.gw(); got = []
        g.provider = lambda m, msgs, mt, **k: (got.append(k), self.OK)[1]
        g.call("qa", self.M, self.MSG, 50, temperature=0.2); self.assertEqual(got, [{"temperature": 0.2}])

    def test_without_temperature_provider_gets_no_extra_argument(self):
        g = self.gw(); got = []
        g.provider = lambda m, msgs, mt, **k: (got.append(k), self.OK)[1]
        g.call("qa", self.M, self.MSG, 50); self.assertEqual(got, [{}])

    def test_legacy_three_argument_provider_still_works_without_temperature(self):
        g = self.gw(); g.provider = lambda m, msgs, mt: self.OK
        self.assertEqual(g.call("qa", self.M, self.MSG, 50)["data"], "ok")


class TestConfigTemperature(unittest.TestCase):
    def cfg(self, t):
        c = copy.deepcopy(CFG); c["agents"]["master_coder"]["temperature"] = t; return c

    def test_valid(self):
        for t in (0, 0.2, 2): C.validate_deep(C.validate(self.cfg(t)))

    def test_invalid(self):
        for t in (-0.1, 2.1, "0.2", True):
            with self.assertRaises(C.ConfigError, msg=repr(t)): C.validate_deep(C.validate(self.cfg(t)))


if __name__ == "__main__": unittest.main()
