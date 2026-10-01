"""Rate Limiter (1.7, 7): modellenkénti token bucket, gördülő ablak, foglalás -> korrekció, napi kvóta, 429/Retry-After."""
from __future__ import annotations
import time, threading, itertools
from collections import deque

class RequestTooLarge(Exception):
    """Egy kérés nagyobb a max_request_share által engedettnél: a Supervisornak bontania kell (1.7)."""

class WaitTooLong(Exception):
    """A várakozás > max_wait_seconds: ESCALATE_HUMAN."""
    def __init__(self, wait): super().__init__(f"várakozás {wait:.0f}s"); self.wait = wait

class DailyQuotaExhausted(Exception):
    """PAUSED_DAILY_LIMIT: a szál checkpointot ment, nullázásig vár vagy emberi döntés (7/4)."""

class RateLimiter:
    def __init__(self, cfg: dict, state=None, clock=time.time):
        r = cfg["rate_limit"]
        self.window = r["window_seconds"]
        self.margin = r["safety_margin"]
        self.share = r["max_request_share"]
        self.max_wait = r["max_wait_seconds"]
        self.per_model = r["per_model"]
        self.priority = r.get("priority_order", [])
        self.aging = r.get("starvation_aging_seconds", 120)
        self.max_concurrent = int(r.get("max_concurrent_calls", 2))
        self.state = state
        self.clock = clock
        self._lock = threading.Lock()
        self._cv = threading.Condition()           # párhuzamossági rések (slot) és prioritásos sor
        self._active = 0
        self._waiters: list[dict] = []
        self._seq = itertools.count()
        self._win: dict[str, deque] = {}       # model -> deque[(ts, tokens, handle)]
        self._req: dict[str, deque] = {}       # model -> deque[ts]
        self._penalty_until: dict[str, float] = {}
        self._next = 0

    def _quota(self, model):
        if model not in self.per_model:
            raise KeyError(f"ismeretlen modell a rate_limit.per_model-ben: {model}")
        return self.per_model[model]

    def _prune(self, model, now):
        w = self._win.setdefault(model, deque())
        while w and w[0][0] <= now - self.window:
            w.popleft()
        q = self._req.setdefault(model, deque())
        while q and q[0] <= now - self.window:
            q.popleft()

    def effective_priority(self, agent: str, waited_s: float) -> float:
        """Kisebb = előrébb. Az öregedés (aging) lépésenként előre viszi a várakozót (éheztetés ellen)."""
        base = self.priority.index(agent) if agent in self.priority else len(self.priority)
        return base - int(waited_s // self.aging)

    def try_reserve(self, model: str, est_tokens: int, now: float | None = None):
        """Visszatér (handle, 0) ha foglalt; (None, várakozási_idő) ha várni kell. Kivételek: RequestTooLarge, DailyQuotaExhausted."""
        now = self.clock() if now is None else now
        q = self._quota(model)
        cap = q["tpm"] * self.margin
        if est_tokens > cap * self.share:
            raise RequestTooLarge(f"{est_tokens} > {cap * self.share:.0f} (max_request_share)")
        if q.get("tpd"):
            used, _ = self.state.get_daily(model, now) if self.state else (0, 0)
            if used + est_tokens > q["tpd"] * self.margin:
                raise DailyQuotaExhausted(model)
        if q.get("rpd") and self.state:
            _, reqs = self.state.get_daily(model, now)
            if reqs + 1 > q["rpd"] * self.margin:
                raise DailyQuotaExhausted(model)
        with self._lock:
            self._prune(model, now)
            pen = self._penalty_until.get(model, 0)
            if pen > now:
                return None, pen - now
            w, rq = self._win[model], self._req[model]
            used = sum(t for _, t, _ in w)
            wait = 0.0
            if used + est_tokens > cap:
                need, acc = used + est_tokens - cap, 0
                for ts, t, _ in w:
                    acc += t
                    if acc >= need:
                        wait = max(wait, ts + self.window - now); break
            if len(rq) + 1 > q["rpm"] * self.margin:
                wait = max(wait, rq[0] + self.window - now)
            if wait > 0:
                return None, wait
            self._next += 1
            h = (self._next, model)
            w.append((now, est_tokens, h)); rq.append(now)
            return h, 0.0

    def reserve(self, model, est_tokens, sleep=time.sleep):
        """Blokkoló foglalás. A várakozás nem hiba és nem iteráció (1.7/3)."""
        waited = 0.0
        while True:
            h, wait = self.try_reserve(model, est_tokens)
            if h:
                return h
            if waited + wait > self.max_wait:
                raise WaitTooLong(waited + wait)
            sleep(wait + 0.01); waited += wait + 0.01

    def settle(self, handle, actual_tokens: int, now: float | None = None):
        """Hívás után: a valós fogyasztás elszámolása, a felesleges foglalás visszakerül (1.7/4)."""
        now = self.clock() if now is None else now
        with self._lock:
            w = self._win.get(handle[1], deque())
            for i, (ts, t, h) in enumerate(w):
                if h == handle:
                    w[i] = (ts, actual_tokens, h); break
        if self.state:
            self.state.add_daily(handle[1], actual_tokens, 1, now)

    def on_provider_429(self, model: str, retry_after: float, now: float | None = None):
        now = self.clock() if now is None else now
        with self._lock:
            self._penalty_until[model] = max(self._penalty_until.get(model, 0), now + retry_after)

    def sync_from_headers(self, model: str, remaining_tokens: int, now: float | None = None):
        """x-ratelimit-remaining-tokens alapján igazítja a helyi számlálót (7/2)."""
        now = self.clock() if now is None else now
        cap = self._quota(model)["tpm"]
        with self._lock:
            self._prune(model, now)
            w = self._win[model]
            used_local = sum(t for _, t, _ in w)
            used_remote = max(0, cap - remaining_tokens)
            if used_remote > used_local:  # csak szigorodhat: nem hiszünk a túl optimista helyi számlálónak
                w.append((now, used_remote - used_local, (-1, model)))

    # --- párhuzamosság és prioritásos várakozósor (1.7, 7): max_concurrent_calls, aging az éheztetés ellen ---
    def _best_waiter(self, now: float):
        return min(self._waiters, key=lambda w: (self.effective_priority(w["agent"], now - w["since"]), w["n"]), default=None)

    def acquire_slot(self, agent: str, timeout: float | None = None) -> None:
        """Egy LLM-hívási rés (slot) megszerzése. Ha tele van, a várakozók között az effektív prioritás dönt
        (alap sorrend + aging), egyenlőség esetén az érkezési sorrend. Timeout -> WaitTooLong."""
        timeout = self.max_wait if timeout is None else timeout
        w = {"agent": agent, "since": self.clock(), "n": next(self._seq)}
        deadline = time.monotonic() + timeout
        with self._cv:
            self._waiters.append(w)
            try:
                while True:
                    if self._active < self.max_concurrent and self._best_waiter(self.clock()) is w:
                        self._active += 1
                        return
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise WaitTooLong(timeout)
                    self._cv.wait(min(left, 0.05))     # rövid ébresztés: az aging idővel átrendezheti a sort
            finally:
                self._waiters.remove(w)
                self._cv.notify_all()

    def release_slot(self) -> None:
        with self._cv:
            if self._active > 0:
                self._active -= 1
            self._cv.notify_all()

    @property
    def active_calls(self) -> int:
        with self._cv:
            return self._active
