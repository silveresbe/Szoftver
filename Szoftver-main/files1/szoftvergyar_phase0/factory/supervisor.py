from .lane_classifier import LaneClassifier


class Supervisor:
    def __init__(self, cfg, state, audit, kill=None, sandbox=None, metrics=None):
        self.cfg, self.state, self.audit, self.kill, self.sandbox = cfg, state, audit, kill, sandbox
        self.metrics = metrics
        self.phase = cfg["rollout"]["phase"]
        self.lane_classifier = LaneClassifier()
        ip = cfg["iteration_policy"]
        self.caps = ip.get("caps") or DEFAULT_CAPS
