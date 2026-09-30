from __future__ import annotations

import random
import sys
import threading
import time
import urllib.parse
import urllib.robotparser

import requests
import urllib3

from .constants import TIMEOUT, UA

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_sessions = threading.local()
_ROBOTS_CACHE: dict[str, object] = {}
_ROBOTS_LOCK = threading.Lock()
_SSL_WARNED: set[str] = set()
_SSL_LOCK = threading.Lock()


def sleep_jitter(lo=0.8, hi=1.8):
    time.sleep(random.uniform(lo, hi))


def session() -> requests.Session:
    s = getattr(_sessions, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept-Language": "en,de;q=0.8"})
        _sessions.s = s
    return s


def get(url: str):
    try:
        return session().get(url, timeout=(4, TIMEOUT), allow_redirects=True)
    except requests.exceptions.SSLError as exc:
        host = urllib.parse.urlsplit(url).netloc
        with _SSL_LOCK:
            first = host not in _SSL_WARNED
            _SSL_WARNED.add(host)
        if first:
            print(f"[ssl] {host}: невалидный сертификат, retry без verify "
                  f"({type(exc).__name__})", file=sys.stderr)
        return session().get(url, timeout=(4, TIMEOUT), verify=False,
                             allow_redirects=True)


def robots_allows(url: str) -> bool:
    p = urllib.parse.urlsplit(url)
    origin = f"{p.scheme}://{p.netloc}"
    with _ROBOTS_LOCK:
        cached = origin in _ROBOTS_CACHE
        rp = _ROBOTS_CACHE.get(origin)
    if not cached:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(origin + "/robots.txt")
        try:
            r = get(origin + "/robots.txt")
            if r.status_code == 200 and len(r.text) < 200_000:
                rp.parse(r.text.splitlines())
            else:
                rp.allow_all = True
        except Exception:
            rp.allow_all = True
        with _ROBOTS_LOCK:
            _ROBOTS_CACHE.setdefault(origin, rp)
            rp = _ROBOTS_CACHE[origin]
    try:
        return rp.can_fetch(UA, url)
    except Exception:
        return True


def origin_variants(origin: str) -> list[str]:
    p = urllib.parse.urlsplit(origin)
    host = p.netloc
    no_www = host[4:] if host.startswith("www.") else host
    out = []
    for h in dict.fromkeys([host, no_www]):
        out.append(f"https://{h}")
        out.append(f"http://{h}")
    return out


def working_origin(origin: str, verbose=False) -> str | None:
    for cand in origin_variants(origin):
        try:
            r = get(cand + "/")
            if r.status_code < 500:
                return cand
        except Exception as e:
            if verbose:
                print(f"  ? {cand}: {type(e).__name__}", file=sys.stderr)
    return None


def is_html_response(r) -> bool:
    if r.status_code != 200:
        return False
    ct = (r.headers.get("Content-Type") or "").lower()
    if ct and "html" not in ct and "xml" not in ct:
        return False
    return True
