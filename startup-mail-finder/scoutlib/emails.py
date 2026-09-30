from __future__ import annotations

import html as htmlmod
import re
import smtplib
import socket
import threading
import urllib.parse

from .constants import (
    BAD, BAD_ROLEBOX, DEOBF, EMAIL_RE, FILE_SUFFIX_RE, FREEMAIL,
    PLACEHOLDER_EMAIL, ROLE_PRIORITY,
)


def clean_url(u: str) -> str | None:
    if not u:
        return None
    u = u.strip()
    if not u.startswith(("http://", "https://")):
        u = "https://" + u.lstrip("/")
    try:
        p = urllib.parse.urlsplit(u)
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.netloc or "." not in p.netloc:
        return None
    return f"{p.scheme}://{p.netloc}"


def deobfuscate(text: str) -> str:
    for pat, rep in DEOBF:
        text = pat.sub(rep, text)
    return text


def normalize_email(raw: str) -> str | None:
    e = htmlmod.unescape(deobfuscate(raw)).strip().strip(".,;:!?\"'()[]<>").lower()
    e = re.sub(r"^mailto:", "", e)
    e = e.split("?", 1)[0]
    if not EMAIL_RE.fullmatch(e):
        return None
    if FILE_SUFFIX_RE.search(e) or BAD.search(e) or BAD_ROLEBOX.match(e):
        return None
    if PLACEHOLDER_EMAIL.search(e):
        return None
    local, _, dom = e.partition("@")
    if not local or dom.count(".") == 0 or len(dom) < 4:
        return None
    if ".." in e or local.startswith(".") or local.endswith("."):
        return None
    return e


def role_score(email: str) -> int:
    for pat, score in ROLE_PRIORITY:
        if pat.search(email):
            return score
    return 30


def email_score(email: str, origin: str) -> int:
    score = role_score(email)
    dom = email.split("@")[-1]
    site = urllib.parse.urlsplit(origin).netloc.lower().removeprefix("www.")
    if dom == site or site.endswith("." + dom) or dom.endswith("." + site):
        score += 40
    elif any(dom.endswith(d) for d in FREEMAIL):
        score += 15
    else:
        score -= 25
    return score


_MX_CACHE: dict[str, bool | None] = {}
_MX_LOCK = threading.Lock()
_SMTP_CACHE: dict[str, str] = {}
_SMTP_LOCK = threading.Lock()


def has_mx(domain: str) -> bool | None:
    with _MX_LOCK:
        if domain in _MX_CACHE:
            return _MX_CACHE[domain]
    result: bool | None
    try:
        import dns.resolver
        result = False
        for t in ("MX", "A"):
            try:
                if dns.resolver.resolve(domain, t, lifetime=4):
                    result = True
                    break
            except Exception:
                continue
    except ImportError:
        result = None
    with _MX_LOCK:
        _MX_CACHE[domain] = result
    return result


def mx_hosts(domain: str) -> list[str]:
    try:
        import dns.resolver
        answers = dns.resolver.resolve(domain, "MX", lifetime=4)
        return [str(r.exchange).rstrip(".") for r in sorted(answers, key=lambda x: x.preference)]
    except Exception:
        return []


def smtp_probe(email: str, helo_host: str = "mail.example.com") -> str:
    """SMTP RCPT TO. Возвращает: ok / reject / catch-all / unknown.

    Не отправляет письмо: только EHLO/MAIL FROM/RCPT TO/RSET/QUIT.
    Catch-all MX (принимает любой local-part) помечается отдельно —
    такой ответ не доказывает, что ящик существует.
    """
    email = (email or "").strip().lower()
    if "@" not in email:
        return "unknown"
    with _SMTP_LOCK:
        if email in _SMTP_CACHE:
            return _SMTP_CACHE[email]
    domain = email.split("@", 1)[1]
    hosts = mx_hosts(domain)
    if not hosts:
        status = "unknown" if has_mx(domain) is None else "reject"
        with _SMTP_LOCK:
            _SMTP_CACHE[email] = status
        return status

    probe_fake = f"no-such-user-zzz-{id(email) % 10_000}@{domain}"
    status = "unknown"
    for host in hosts[:2]:
        try:
            with smtplib.SMTP(timeout=8) as smtp:
                smtp.connect(host, 25)
                smtp.ehlo_or_helo_if_needed()
                smtp.mail(f"probe@{helo_host}")
                code_real, _ = smtp.rcpt(email)
                smtp.rset()
                smtp.mail(f"probe@{helo_host}")
                code_fake, _ = smtp.rcpt(probe_fake)
                if 200 <= code_real < 300 and 200 <= code_fake < 300:
                    status = "catch-all"
                elif 200 <= code_real < 300:
                    status = "ok"
                elif code_real in (550, 551, 552, 553, 554):
                    status = "reject"
                else:
                    status = "unknown"
                break
        except (smtplib.SMTPException, socket.timeout, OSError):
            continue
    with _SMTP_LOCK:
        _SMTP_CACHE[email] = status
    return status
