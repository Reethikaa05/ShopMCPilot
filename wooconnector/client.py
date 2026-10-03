"""Resilient, read-only WooCommerce REST client."""
from __future__ import annotations
import random, time, threading
from urllib.parse import urlparse
import httpx
from .auth import Credentials
from .audit import Audit
from .ratelimit import AdaptiveLimiter

API_PREFIX = "/wp-json/wc/v3"
RETRY_STATUS = {429, 502, 503, 504}


class WooError(Exception):
    retryable = False
    hint = ""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class WooAuthError(WooError):
    hint = "Credentials rejected. Ask the merchant to re-run the connect flow or issue a new read-only key."


class WooNotFound(WooError):
    hint = "That ID does not exist. Use a list/search tool to find valid IDs."


class WooRateLimited(WooError):
    retryable = True
    hint = "Store is throttling us. Narrow the query (filters, smaller per_page) or retry later."


class WooAPIError(WooError):
    retryable = True


class ReadOnlyViolation(WooError):
    hint = "This connector never writes to the store."


class WooClient:
    def __init__(self, creds: Credentials, *, limiter: AdaptiveLimiter | None = None, audit: Audit | None = None,
                 max_retries=5, backoff_base=0.5, cache_ttl=15.0, timeout=20.0, allow_insecure=False,
                 transport=None, sleep=time.sleep):
        host = urlparse(creds.base_url)
        if host.scheme != "https" and not allow_insecure and host.hostname not in ("127.0.0.1", "localhost"):
            raise WooError("Refusing plain-http store URL (credentials would travel in clear text). Use https.")
        self.creds = creds
        self.limiter = limiter or AdaptiveLimiter()
        self.audit = audit or Audit()
        self.max_retries, self.backoff_base, self.cache_ttl, self._sleep = max_retries, backoff_base, cache_ttl, sleep
        self._cache: dict = {}
        self._http = httpx.Client(timeout=timeout, transport=transport, headers={"User-Agent": "agent-studio-woo-connector/1.0"})
        self.stats = {"requests": 0, "retries": 0, "cache_hits": 0, "errors": 0}

    # -- core ---------------------------------------------------------------
    def get(self, path: str, params: dict | None = None, *, use_cache=True):
        """GET -> (json, headers). Only GET exists on purpose."""
        params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        url = f"{self.creds.base_url}{API_PREFIX}{path}"
        key = (url, tuple(sorted(params.items())))
        if use_cache and self.cache_ttl:
            hit = self._cache.get(key)
            if hit and hit[0] > time.monotonic():
                self.stats["cache_hits"] += 1
                self.audit.record("http", method="GET", path=path, status=200, ms=0, cached=True, retries=0)
                return hit[1], hit[2]
        retries, t0 = 0, time.perf_counter()
        for attempt in range(self.max_retries + 1):
            self.limiter.acquire()
            self.stats["requests"] += 1
            try:
                r = self._http.get(url, params=params, auth=(self.creds.consumer_key, self.creds.consumer_secret))
            except httpx.TransportError as e:
                if attempt == self.max_retries:
                    self.stats["errors"] += 1
                    raise WooAPIError(f"Network error: {e}") from e
                retries += 1; self.stats["retries"] += 1
                self._sleep(self._backoff(attempt)); continue
            if r.status_code in RETRY_STATUS:
                ra = self._retry_after(r)
                self.limiter.on_throttle(ra if ra is not None else self._backoff(attempt))
                self.audit.record("throttle", path=path, status=r.status_code, retry_after=ra, attempt=attempt + 1)
                if attempt == self.max_retries:
                    self.stats["errors"] += 1
                    raise WooRateLimited(f"Gave up after {attempt + 1} attempts (HTTP {r.status_code})", r.status_code)
                retries += 1; self.stats["retries"] += 1
                continue  # limiter.acquire() enforces the wait
            self.limiter.on_success()
            ms = round((time.perf_counter() - t0) * 1000, 1)
            self.audit.record("http", method="GET", path=path, status=r.status_code, ms=ms, retries=retries, cached=False)
            if r.status_code in (401, 403):
                self.stats["errors"] += 1; raise WooAuthError(self._msg(r), r.status_code)
            if r.status_code == 404:
                raise WooNotFound(self._msg(r), 404)
            if r.status_code >= 400:
                self.stats["errors"] += 1; raise WooAPIError(self._msg(r), r.status_code)
            data, headers = r.json(), dict(r.headers)
            if use_cache and self.cache_ttl:
                self._cache[key] = (time.monotonic() + self.cache_ttl, data, headers)
                if len(self._cache) > 256:
                    self._cache.pop(next(iter(self._cache)))
            return data, headers
        raise WooAPIError("unreachable")

    def request(self, method: str, *a, **k):
        raise ReadOnlyViolation(f"{method} blocked: read-only connector")

    def paginate(self, path: str, params: dict | None = None, *, per_page=100, max_items=1000):
        """Yield items across pages, stopping at max_items (hard guard for agent safety)."""
        page, n = 1, 0
        while True:
            data, h = self.get(path, {**(params or {}), "page": page, "per_page": per_page})
            for item in data:
                yield item
                n += 1
                if n >= max_items:
                    return
            total_pages = int({k.lower(): v for k, v in h.items()}.get("x-wp-totalpages", page))
            if page >= total_pages or not data:
                return
            page += 1

    # -- helpers ------------------------------------------------------------
    def _backoff(self, attempt: int) -> float:
        return min(30.0, self.backoff_base * 2 ** attempt) * random.uniform(0.7, 1.3)

    @staticmethod
    def _retry_after(r) -> float | None:
        v = r.headers.get("Retry-After")
        try:
            return float(v) if v is not None else None
        except ValueError:
            return None

    @staticmethod
    def _msg(r) -> str:
        try:
            return r.json().get("message", r.text[:200])
        except Exception:
            return r.text[:200]

    def close(self):
        self._http.close()
