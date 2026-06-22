"""Shared HTTP client: per-host rate limiting, retry/backoff, robots.txt.

Every source fetcher and the live-checker share one ``HttpClient`` so politeness
limits are enforced globally per host (spec Section 12).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import requests

log = logging.getLogger("internfinder.http")


@dataclass
class HttpResult:
    """A normalized result that records the *final* URL after redirects."""

    ok: bool
    status: int
    url: str            # final URL (after redirects)
    requested_url: str  # original URL
    text: str
    redirected: bool
    error: str = ""

    def json(self) -> Any:
        import json

        return json.loads(self.text)


class HttpClient:
    def __init__(self, http_cfg: dict[str, Any]):
        self.user_agent = http_cfg.get("user_agent", "internfinder/1.0")
        self.timeout = float(http_cfg.get("request_timeout", 20))
        self.rate_limit = float(http_cfg.get("rate_limit_per_host_sec", 1.0))
        self.max_retries = int(http_cfg.get("max_retries", 3))
        self.backoff_base = float(http_cfg.get("backoff_base_sec", 1.5))
        self.respect_robots = bool(http_cfg.get("respect_robots", True))

        self._session = requests.Session()
        self._session.headers.update({"User-Agent": self.user_agent})
        self._lock = threading.Lock()
        self._last_hit: dict[str, float] = {}        # host -> monotonic ts
        self._robots: dict[str, Optional[RobotFileParser]] = {}

    # ------------------------------------------------------------- politeness
    def _host(self, url: str) -> str:
        return urlsplit(url).netloc.lower()

    def _throttle(self, host: str) -> None:
        with self._lock:
            now = time.monotonic()
            last = self._last_hit.get(host, 0.0)
            wait = self.rate_limit - (now - last)
            if wait > 0:
                time.sleep(wait)
            self._last_hit[host] = time.monotonic()

    def can_fetch(self, url: str) -> bool:
        """Honor robots.txt. On any error fetching robots, default to allow."""
        if not self.respect_robots:
            return True
        host = self._host(url)
        rp = self._robots.get(host, "missing")
        if rp == "missing":
            rp = self._load_robots(url)
            self._robots[host] = rp
        if rp is None:
            return True
        try:
            return rp.can_fetch(self.user_agent, url)
        except Exception:
            return True

    def _load_robots(self, url: str) -> Optional[RobotFileParser]:
        parts = urlsplit(url)
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        rp = RobotFileParser()
        try:
            self._throttle(self._host(url))
            resp = self._session.get(robots_url, timeout=self.timeout)
            if resp.status_code >= 400:
                return None  # no usable robots.txt -> allow
            rp.parse(resp.text.splitlines())
            return rp
        except Exception as exc:  # network error -> allow, but log once
            log.debug("robots.txt fetch failed for %s: %s", robots_url, exc)
            return None

    # ----------------------------------------------------------------- request
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[dict] = None,
        params: Optional[dict] = None,
        json_body: Any = None,
        allow_redirects: bool = True,
        obey_robots: bool = True,
        timeout: Optional[float] = None,
    ) -> HttpResult:
        if obey_robots and not self.can_fetch(url):
            log.info("robots.txt disallows %s — skipping", url)
            return HttpResult(False, 0, url, url, "", False, error="blocked_by_robots")

        attempt = 0
        last_err = ""
        while attempt <= self.max_retries:
            attempt += 1
            self._throttle(self._host(url))
            try:
                resp = self._session.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_body,
                    allow_redirects=allow_redirects,
                    timeout=timeout or self.timeout,
                )
            except requests.RequestException as exc:
                last_err = f"{type(exc).__name__}: {exc}"
                self._sleep_backoff(attempt)
                continue

            # Retry transient server/rate errors.
            if resp.status_code in (429, 500, 502, 503, 504) and attempt <= self.max_retries:
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    time.sleep(min(float(retry_after), 30.0))
                else:
                    self._sleep_backoff(attempt)
                last_err = f"HTTP {resp.status_code}"
                continue

            final_url = str(resp.url)
            return HttpResult(
                ok=resp.ok,
                status=resp.status_code,
                url=final_url,
                requested_url=url,
                text=resp.text,
                redirected=(final_url.rstrip("/") != url.rstrip("/")),
            )

        return HttpResult(False, 0, url, url, "", False, error=last_err or "max_retries_exceeded")

    def _sleep_backoff(self, attempt: int) -> None:
        time.sleep(self.backoff_base ** attempt)

    # ------------------------------------------------------------- convenience
    def get(self, url: str, **kw) -> HttpResult:
        return self.request("GET", url, **kw)

    def get_json(self, url: str, **kw) -> Any:
        res = self.get(url, **kw)
        if not res.ok:
            raise RuntimeError(f"GET {url} -> {res.status or res.error}")
        return res.json()

    def post_json(self, url: str, body: Any, **kw) -> Any:
        res = self.request("POST", url, json_body=body, **kw)
        if not res.ok:
            raise RuntimeError(f"POST {url} -> {res.status or res.error}")
        return res.json()
