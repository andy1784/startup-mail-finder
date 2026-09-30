from __future__ import annotations

import csv
import html as htmlmod
import os
import shutil
import subprocess
import sys
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

from bs4 import BeautifulSoup

from .constants import (
    CHROME_CANDIDATES, CONTACT_HINTS, CONTACT_PATHS, EMAIL_RE,
    MAX_PAGES_PER_SITE, MAX_QUEUE,
)
from .emails import email_score, has_mx, normalize_email, role_score
from .http import get, is_html_response, robots_allows, sleep_jitter, working_origin
from .io import canonical_domain, read_csv, write_csv

_CHROME_SLOTS = threading.Semaphore(2)


def soup_of(html: str):
    return BeautifulSoup(html, "html.parser")


def harvest_emails_from_html(html: str) -> set[str]:
    html = htmlmod.unescape(html)
    found = set()
    soup = soup_of(html)
    for a in soup.find_all("a", href=True):
        if a["href"].lower().startswith("mailto:"):
            e = normalize_email(a["href"])
            if e:
                found.add(e)
    for m in EMAIL_RE.findall(_deobf(soup.get_text(" "))):
        e = normalize_email(m)
        if e:
            found.add(e)
    for m in EMAIL_RE.findall(_deobf(html)):
        e = normalize_email(m)
        if e:
            found.add(e)
    return found


def _deobf(text: str) -> str:
    from .emails import deobfuscate
    return deobfuscate(text)


def candidate_urls(origin: str, soup) -> list[str]:
    urls = []
    origin_host = urllib.parse.urlsplit(origin).netloc.lower().removeprefix("www.")
    for a in soup.find_all("a", href=True):
        href = urllib.parse.urljoin(origin + "/", a["href"])
        p = urllib.parse.urlsplit(href)
        if p.netloc.lower().removeprefix("www.") != origin_host:
            continue
        path = p.path.lower().rstrip("/")
        if any(h in path for h in CONTACT_HINTS) and path:
            urls.append(href.split("#")[0])
    for p_ in CONTACT_PATHS:
        urls.append(origin + p_)
    seen, out = set(), []
    for u in urls:
        k = urllib.parse.urlsplit(u).path.rstrip("/") or "/"
        if k not in seen:
            seen.add(k)
            out.append(u)
    return out[:MAX_QUEUE]


def render_with_js(url: str, budget_ms=6000) -> str | None:
    exe = next((p for p in CHROME_CANDIDATES if os.path.exists(p)), None) \
        or shutil.which("chromium") or shutil.which("google-chrome")
    if not exe:
        return None
    try:
        with _CHROME_SLOTS:
            r = subprocess.run(
                [exe, "--headless", "--no-sandbox", "--disable-gpu",
                 "--disable-dev-shm-usage", "--hide-scrollbars",
                 f"--virtual-time-budget={budget_ms}", "--dump-dom", url],
                capture_output=True, timeout=budget_ms / 1000 + 20, text=True)
        return r.stdout if r.stdout and "<" in r.stdout else None
    except Exception:
        return None


def crawl_domain(company: dict, respect_robots=True, verbose=False,
                 js=False) -> dict:
    origin = working_origin(company["website"], verbose) or company["website"]
    out = dict(company)
    emails, pages = set(), 0
    if company.get("osm_email"):
        emails.add(company["osm_email"])

    queue = [origin + "/"]
    tried = set()
    while queue and pages < MAX_PAGES_PER_SITE:
        url = queue.pop(0)
        key = url.split("#")[0]
        if key in tried:
            continue
        tried.add(key)
        if respect_robots and not robots_allows(key):
            continue
        try:
            r = get(key)
        except Exception as e:
            if verbose:
                print(f"  ! {key}: {e}", file=sys.stderr)
            continue
        pages += 1
        if not is_html_response(r):
            continue
        emails |= harvest_emails_from_html(r.text)
        if pages == 1:
            queue += candidate_urls(origin, soup_of(r.text))
        sleep_jitter(0.4, 1.0)

    js_used = ""
    if not emails and js:
        for url in (origin + "/", origin + "/contact"):
            if respect_robots and not robots_allows(url):
                continue
            html = render_with_js(url)
            if not html:
                continue
            found = harvest_emails_from_html(html)
            if found:
                emails |= found
                js_used = url
                break
            sleep_jitter(0.4, 1.0)

    scored = sorted(emails, key=lambda e: -email_score(e, origin))
    out["emails"] = ";".join(scored)
    out["email"] = scored[0] if scored else ""
    top = role_score(scored[0]) if scored else 0
    edom = scored[0].split("@")[-1] if scored else ""
    site_dom = urllib.parse.urlsplit(origin).netloc.lower().removeprefix("www.")
    on_site = bool(edom) and (edom == site_dom or site_dom.endswith("." + edom)
                              or edom.endswith("." + site_dom))
    out["email_kind"] = ("hr" if top >= 100 else
                         "role" if top >= 60 else
                         "named" if scored else "")
    out["on_site"] = "y" if on_site else "n"
    out["note"] = "" if emails else "no_public_email (проверь /careers или LinkedIn)"
    out["mx"] = "ok" if scored and has_mx(scored[0].split("@")[1]) else ""
    out["pages_crawled"] = pages
    out["js_rendered"] = js_used
    out["website"] = origin
    out["source_domain"] = company["website"]
    return out


def _done_domains(path: str) -> dict[str, dict]:
    if not path or not os.path.exists(path):
        return {}
    out = {}
    for row in read_csv(path):
        key = canonical_domain(row.get("domain") or row.get("website") or row.get("source_domain") or "")
        if key:
            out[key] = row
    return out


def cmd_emails(args):
    with open(args.input, newline="", encoding="utf-8") as f:
        companies = list(csv.DictReader(f))
    companies = [c for c in companies if c.get("website")]
    if not companies:
        sys.exit("[emails] в CSV нет строк с колонкой website")

    done = _done_domains(args.out) if getattr(args, "resume", False) else {}
    todo = []
    for c in companies:
        key = canonical_domain(c.get("domain") or c.get("website") or "")
        if key in done:
            continue
        todo.append(c)
    print(f"[emails] обход {len(todo)} сайтов"
          + (f" (пропуск {len(done)} уже в {args.out})" if done else "")
          + f", workers={args.workers}",
          file=sys.stderr)

    results = list(done.values())
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {ex.submit(crawl_domain, c, not args.ignore_robots,
                          args.verbose, args.js): c
                for c in todo}
        for i, fu in enumerate(as_completed(futs), 1):
            try:
                results.append(fu.result())
            except Exception as e:
                print(f"  ! {futs[fu].get('name')}: {e}", file=sys.stderr)
            if i % 10 == 0:
                print(f"  …{i}/{len(todo)}", file=sys.stderr)

    results.sort(key=lambda r: (not bool(r.get("email")),
                                {"hr": 0, "role": 1}.get(r.get("email_kind"), 2),
                                r.get("name", "").lower()))
    fields = ["name", "email", "email_kind", "on_site", "mx", "emails", "website",
              "phone", "city", "address", "tags", "pages_crawled", "js_rendered",
              "note"]
    fields = [k for k in fields if any(k in r for r in results)] + \
             [k for k in results[0].keys() if k not in fields] if results else fields
    write_csv(args.out, results, fields)
    found = sum(1 for r in results if r.get("email"))
    print(f"[emails] найдено email: {found}/{len(results)} -> {args.out}",
          file=sys.stderr)
