"""Polite HTTP: one shared session, per-host rate limit, robots.txt, size caps."""
from __future__ import annotations

import logging
import threading
import time
import urllib.robotparser
from typing import Optional
from urllib.parse import urlparse

import requests

from . import config

log = logging.getLogger(__name__)

_local = threading.local()
_host_lock = threading.Lock()
_host_next: dict[str, float] = {}
_robots: dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}
_robots_lock = threading.Lock()


def session() -> requests.Session:
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update({
            "User-Agent": config.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        _local.session = s
    return s


def _wait_turn(host: str) -> None:
    while True:
        with _host_lock:
            now = time.monotonic()
            nxt = _host_next.get(host, 0.0)
            if now >= nxt:
                _host_next[host] = now + config.PER_HOST_DELAY
                return
            delay = nxt - now
        time.sleep(delay)


def allowed(url: str) -> bool:
    if not config.RESPECT_ROBOTS:
        return True
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    with _robots_lock:
        if base not in _robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = session().get(base + "/robots.txt", timeout=10)
                if r.status_code == 200 and "text" in r.headers.get("content-type", "text"):
                    rp.parse(r.text.splitlines())
                    _robots[base] = rp
                else:
                    _robots[base] = None  # no robots.txt → allowed
            except requests.RequestException:
                _robots[base] = None
        rp = _robots[base]
    return True if rp is None else rp.can_fetch(config.USER_AGENT, url)


_stats_local = threading.local()


def stats() -> dict:
    """Per-thread counters (reset with reset_stats) for discovery diagnostics."""
    st = getattr(_stats_local, "d", None)
    if st is None:
        st = _stats_local.d = {}
    return st


def reset_stats() -> None:
    _stats_local.d = {}


def _count(key: str) -> None:
    st = stats()
    st[key] = st.get(key, 0) + 1


class Fetched:
    def __init__(self, url: str, status: int, content_type: str, body: bytes):
        self.url, self.status, self.content_type, self.body = url, status, content_type, body

    @property
    def text(self) -> str:
        for enc in ("utf-8", "latin-1"):
            try:
                return self.body.decode(enc)
            except UnicodeDecodeError:
                continue
        return self.body.decode("utf-8", "replace")

    @property
    def is_html(self) -> bool:
        return "html" in self.content_type or self.body[:200].lstrip().lower().startswith((b"<!doctype", b"<html"))


def fetch(url: str, *, max_bytes: int = config.MAX_DOWNLOAD_BYTES, check_robots: bool = True,
          timeout: int = config.REQUEST_TIMEOUT, headers: Optional[dict] = None) -> Optional[Fetched]:
    """GET with rate limit + robots + size cap. Returns None on any failure."""
    if check_robots and not allowed(url):
        log.debug("robots.txt disallows %s", url)
        _count("robots_blocked")
        return None
    host = urlparse(url).netloc.lower()
    _wait_turn(host)
    try:
        with session().get(url, timeout=timeout, stream=True, allow_redirects=True, headers=headers) as r:
            _count(str(r.status_code))
            if r.status_code >= 400:
                return Fetched(r.url, r.status_code, r.headers.get("content-type", ""), b"")
            chunks, size = [], 0
            for chunk in r.iter_content(64 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    log.debug("Too large, skipping: %s", url)
                    return None
                chunks.append(chunk)
            return Fetched(r.url, r.status_code, r.headers.get("content-type", "").lower(), b"".join(chunks))
    except requests.RequestException as exc:
        log.debug("Fetch failed %s: %s", url, exc)
        _count("conn_error")
        return None
