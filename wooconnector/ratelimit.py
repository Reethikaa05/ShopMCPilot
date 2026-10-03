"""Adaptive token-bucket limiter (AIMD).

* Normal operation: smooth requests at `rate` req/s with `burst` capacity.
* On 429/503: multiplicative decrease (rate/2) and honour Retry-After.
* On success: additive increase back toward `max_rate`.
This is the same congestion-control idea TCP uses, applied to a merchant's shared host,
so an agent can never turn a busy sale day into an outage for the store.
"""
from __future__ import annotations
import threading, time


class AdaptiveLimiter:
    def __init__(self, rate=8.0, burst=8, min_rate=0.5, max_rate=None, clock=time.monotonic, sleep=time.sleep):
        self.max_rate = max_rate or rate
        self.rate, self.burst, self.min_rate = float(rate), float(burst), float(min_rate)
        self._tokens = float(burst)
        self._clock, self._sleep = clock, sleep
        self._last = clock()
        self._blocked_until = 0.0
        self._last_cut = -1e9
        self._lock = threading.Lock()
        self.throttle_events = 0
        self.waited_s = 0.0
        self.acquired = 0

    def acquire(self) -> float:
        """Block until a token is available. Returns seconds waited."""
        total = 0.0
        while True:
            with self._lock:
                now = self._clock()
                self._tokens = min(self.burst, self._tokens + (now - self._last) * self.rate)
                self._last = now
                wait = max(self._blocked_until - now, 0.0)
                if wait == 0.0:
                    if self._tokens >= 1:
                        self._tokens -= 1
                        self.acquired += 1
                        self.waited_s += total
                        return total
                    wait = (1 - self._tokens) / self.rate
            self._sleep(wait)
            total += wait

    def on_success(self):
        with self._lock:
            self.rate = min(self.max_rate, self.rate + 0.25 * self.max_rate / 8)

    def on_throttle(self, retry_after: float = 1.0):
        with self._lock:
            self.throttle_events += 1
            now = self._clock()
            if now - self._last_cut > 1.0:  # debounce: one burst of N parallel 429s = ONE multiplicative cut
                self.rate = max(self.min_rate, self.rate / 2)
                self._last_cut = now
            self._tokens = 0.0
            self._blocked_until = max(self._blocked_until, self._clock() + max(retry_after, 0.0))

    def stats(self) -> dict:
        with self._lock:
            return {"rate_per_s": round(self.rate, 2), "max_rate_per_s": self.max_rate,
                    "tokens": round(self._tokens, 2), "throttle_events": self.throttle_events,
                    "total_wait_s": round(self.waited_s, 2), "acquired": self.acquired}
