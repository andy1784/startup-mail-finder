from __future__ import annotations

import csv
import json
import os
import re
import sys
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

from .constants import (
    EMAIL_RE, PEOPLE_PAGES, TITLE_RE, UMLAUTS,
)
from .crawl import soup_of
from .emails import normalize_email, smtp_probe
from .http import get, is_html_response, robots_allows, sleep_jitter, working_origin
from .io import canonical_domain, read_csv, write_csv

PERSON_LABELS = re.compile(
    r"(Geschäftsführ(?:er|erin)|Managing Director|CEO|Chief Executive|"
    r"Co-?Founder|Founder|Mitgründer(?:in)?|Inhaber(?:in)?|Owner|Verantwortlich|"
    r"Director|Geschäftsführung|Unternehmensführung|Managing Partner|"
    r"Vertretungsberechtigt)", re.I)
NAME_STOP = re.compile(
    r"\b(?:HRB|HR-Nr|HRA|AGB|Amtsgericht|Registergericht|USt-IdNr|USt|"
    r"Telefon|Tel|Fax|Mail|Email|E-Mail|Web|Website|Register|Portal|Sitz|"
    r"Anschrift|Vertretungsberechtigt|NBank|Kontonummer|BIC|IBAN|"
    r"Versicherungsnummer|Geschäftsführ\w*|Managing\s+Director|Founder|"
    r"Inhaber\w*|Managing\s+Partner|CEO|Mitgründer\w*|Co-?Founder|"
    r"Steuernummer|Mitarbeiter\w*|Team|Beschäftigte)\b|&", re.I)
STOPWORDS = {
    "und", "oder", "der", "die", "das", "ein", "eine", "einen", "einem",
    "im", "am", "bei", "auf", "aus", "den", "dem", "des", "zum", "zur",
    "von", "zu", "durch", "als", "auch", "alle", "nicht", "kein", "keine",
    "jeder", "jede", "diese", "dieser", "dieses", "wir", "sind", "ist",
    "uns", "unser", "ihr", "sich", "für", "mit", "über", "unter", "vor",
    "nach", "seit", "bis", "wird", "werden", "kann", "können", "möchten",
    "gerne", "kontakt", "ansprechpartner", "impressum", "datenschutz",
    "seite", "mehr", "hier", "jetzt", "start", "unternehmen", "firma",
    "gesellschaft", "gmbh", "ag", "ltd", "inc", "llc", "as", "bv", "oy", "ab",
    "sa", "co", "kg", "ohg", "eg", "se", "plc", "srl", "spa", "sas",
    "the", "and", "or", "of", "for", "with", "our", "we", "are", "is", "be",
    "to", "in", "on", "at", "this", "that", "these", "those", "not", "all",
    "any", "can", "may", "write", "here", "click", "mail", "email", "e",
    "contact", "us", "imprint", "privacy", "legal", "more", "about", "team",
    "company", "page", "phone", "tel", "fax", "ceo", "founder",
}
ACTION_WORDS = {"schreiben", "write", "click", "klick", "hier", "here", "jetzt",
                "now", "kontakt", "contact", "aufrufen", "open", "ansehen",
                "view", "mehr", "more", "send", "senden", "mailen"}
LABEL_PREFIX = re.compile(r"^(?:e-?mail|email|mail|write (?:an )?to|contact)\b[\s:]*",
                          re.I)


def _garbage_name(label: str) -> bool:
    if not label:
        return True
    toks = [t.strip(".,&!?").lower() for t in label.split() if t.strip(".,&!?")]
    if not toks:
        return True
    if any(t in ACTION_WORDS for t in toks):
        return True
    flat = [p for t in toks for p in re.split(r"[^a-zäöüß]+", t) if p]
    return all(t in STOPWORDS for t in toks) or all(t in STOPWORDS for t in flat)


def _looks_like_person(tokens: list[str]) -> bool:
    if not 2 <= len(tokens) <= 4:
        return False
    if any(t.lower().strip(".,") in STOPWORDS for t in tokens):
        return False
    if any(len(t) > 22 for t in tokens):
        return False
    return bool(re.match(r"^[A-ZÄÖÜ][a-zäöüßA-Z-]{1,}$", tokens[0]))


def extract_people_from_text(text: str) -> list[tuple[str, str]]:
    out = []
    for m in PERSON_LABELS.finditer(text):
        role = m.group(0)
        tail = text[m.end():m.end() + 160]
        tail = tail.lstrip(" :\t–—-")
        tail = re.sub(r"^(?:is|ist|sind|was|wer|who|that|namely|namely)\b[\s:]*",
                      "", tail, flags=re.I)
        tail = tail.lstrip(" :\t–—-")
        stop = NAME_STOP.search(tail)
        if stop:
            tail = tail[:stop.start()]
        tail = tail.split("\n")[0]
        chunks = [c for c in re.split(r"[,;()]| and | und | is | ist | who | who is ",
                                      tail) if c and c.strip()]
        hit = None
        for chunk in chunks:
            raw = chunk.strip(" .,:;&")
            if not raw or "@" in raw or re.search(r"\d", raw):
                continue
            cleaned = [t.strip(" .&") for t in raw.split() if t.strip(" .&")]
            cleaned = [t for t in cleaned if not TITLE_RE.match(t)]
            if _looks_like_person(cleaned):
                hit = cleaned
                break
        if not hit:
            continue
        out.append((" ".join(hit), role))
    seen, uniq = set(), []
    for name, role in out:
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        uniq.append((name, role))
    return uniq


def _iter_ldjson_nodes(obj):
    if isinstance(obj, list):
        for item in obj:
            yield from _iter_ldjson_nodes(item)
    elif isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _iter_ldjson_nodes(v)


def _ldjson_str(value) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    if isinstance(value, dict):
        value = value.get("@value", "") or value.get("name", "") or value.get("email", "")
    return value if isinstance(value, str) else ""


def extract_people_from_ldjson(soup) -> list[tuple[str, str]]:
    out = []
    for s in soup.find_all("script", type="application/ld+json"):
        raw = s.string or s.get_text() or ""
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for obj in _iter_ldjson_nodes(data):
            name = _ldjson_str(obj.get("name", ""))
            if "@" in name:
                continue
            m = EMAIL_RE.search(_ldjson_str(obj.get("email", "")))
            if not m:
                continue
            e = normalize_email(m.group(0))
            if e:
                out.append((e, name.strip()))
    return out


def _strip_titles(text: str) -> str:
    for _ in range(4):
        before = text
        text = TITLE_RE.sub("", text).lstrip(" .-")
        if text == before:
            break
    return text


def slug_name(name: str) -> tuple[str, str] | None:
    name = _strip_titles(name)
    parts = [p for p in re.split(r"[\s\-]+", name) if p and "@" not in p]
    parts = [p.strip(".,") for p in parts if p.strip(".,")]
    parts = [p for p in parts if not TITLE_RE.match(p)]
    if len(parts) < 2:
        return None
    first, last = parts[0], parts[-1]
    for k, v in UMLAUTS.items():
        first = first.replace(k, v).lower()
        last = last.replace(k, v).lower()
    first = re.sub(r"[^a-z]", "", first)
    last = re.sub(r"[^a-z]", "", last)
    if len(first) < 2 or len(last) < 2:
        return None
    return first, last


def guess_emails(name: str, domain: str) -> list[str]:
    s = slug_name(name)
    if not s:
        return []
    first, last = s
    pats = [f"{first}.{last}", f"{first}", f"{first[0]}{last}", f"{first}{last}",
            f"{first[0]}.{last}", f"{first}{last[0]}", f"{first}_{last}"]
    seen, out = set(), []
    for p in pats:
        e = f"{p}@{domain}"
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out


def crawl_people(company: dict, respect_robots=True, verify_smtp=False) -> list[dict]:
    origin = working_origin(company["website"]) or company["website"]
    dom = urllib.parse.urlsplit(origin).netloc.lower().removeprefix("www.")
    found: dict[str, dict] = {}

    def add(name, role, email, source):
        name = (name or "").strip()
        email = normalize_email(email) if email else ""
        if not name and not email:
            return
        key = (name or email).lower()
        rec = found.setdefault(key, {"name": name, "role": role,
                                     "email_public": email, "source": source})
        if email and not rec["email_public"]:
            rec["email_public"] = email
        if role and not rec["role"]:
            rec["role"] = role
        if source == "ld+json" and rec["source"] != "ld+json":
            rec["source"] = "ld+json"

    for path in PEOPLE_PAGES:
        url = origin + path
        if respect_robots and not robots_allows(url):
            continue
        try:
            r = get(url)
        except Exception:
            continue
        if not is_html_response(r):
            continue
        soup = soup_of(r.text)
        for a in soup.find_all("a", href=True):
            if not a["href"].lower().startswith("mailto:"):
                continue
            e = normalize_email(a["href"])
            if not e:
                continue
            label = a.get_text(" ", strip=True) or a.get("title", "") \
                or a.get("aria-label", "")
            if EMAIL_RE.fullmatch(label or "") or _garbage_name(label):
                label = ""
            if label:
                label = LABEL_PREFIX.sub("", label).strip(" .:;-") or ""
            add(label, "", e, "mailto")
        for e, name in extract_people_from_ldjson(soup):
            add(name, "", e, "ld+json")
        text = re.sub(r"\s+", " ", soup.get_text(" "))
        for name, role in extract_people_from_text(text):
            add(name, role, "", "impressum/about")
        sleep_jitter(0.3, 0.8)

    out = []
    names_per_mail: dict[str, set] = {}
    for rec in found.values():
        if rec.get("name") and rec.get("email_public"):
            names_per_mail.setdefault(rec["email_public"], set()).add(rec["name"])
    shared = {m for m, n in names_per_mail.items() if len(n) > 1}

    for rec in list(found.values()):
        rec = dict(rec)
        if rec.get("email_public") in shared:
            rec["name"] = ""
            rec["role"] = "общий ящик на нескольких сотрудников"
        rec["company"] = company.get("name", "")
        rec["domain"] = dom
        rec["website"] = origin
        guesses = [g for g in guess_emails(rec["name"], dom)
                   if g != rec["email_public"]]
        smtp_status = ""
        kept = []
        if verify_smtp and guesses:
            for g in guesses:
                st = smtp_probe(g)
                if st == "ok":
                    kept.append(g)
                    smtp_status = smtp_status or "ok"
                    break
                if st == "catch-all":
                    smtp_status = smtp_status or "catch-all"
                    kept.append(g)
                elif st == "reject":
                    continue
                else:
                    kept.append(g)
            guesses = kept[:4] if kept else []
            if smtp_status == "ok" and kept:
                rec["email_public"] = rec["email_public"] or kept[0]
        rec["email_guesses"] = ";".join(guesses[:4])
        rec["smtp"] = smtp_status
        if rec["email_public"] and rec["email_public"].endswith("@" + dom):
            rec["confidence"] = "high"
        elif rec["email_public"]:
            rec["confidence"] = "medium"
        elif guesses:
            rec["confidence"] = "guess"
        else:
            rec["confidence"] = ""
        out.append(rec)

    seen, uniq = set(), []
    for r in out:
        k = (r.get("email_public", ""), r.get("name", ""))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)
    return uniq


def cmd_people(args):
    with open(args.input, newline="", encoding="utf-8") as f:
        companies = [c for c in csv.DictReader(f) if c.get("website")]
    seeds = []
    for c in companies:
        for extra in (c.get("founder", ""), c.get("owner", "")):
            for n in re.split(r"[,;/]| and ", extra or ""):
                n = n.strip()
                if n:
                    seeds.append((c, n))
    if not companies:
        sys.exit("[people] в CSV нет строк с колонкой website")

    done_domains: set[str] = set()
    prior: list[dict] = []
    if getattr(args, "resume", False) and os.path.exists(args.out):
        prior = read_csv(args.out)
        done_domains = {canonical_domain(r.get("domain") or r.get("website") or "")
                        for r in prior}
        companies = [c for c in companies
                     if canonical_domain(c.get("domain") or c.get("website") or "")
                     not in done_domains]
        seeds = [(c, n) for c, n in seeds
                 if canonical_domain(c.get("domain") or c.get("website") or "")
                 not in done_domains]

    print(f"[people] {len(companies)} сайтов"
          + (f" (пропуск {len(done_domains)} уже в {args.out})" if done_domains else "")
          + (f" + {len(seeds)} имён из источника" if seeds else ""),
          file=sys.stderr)

    results, lock = [], threading.Lock()
    results.extend(prior)
    verify = getattr(args, "verify_smtp", False)

    def work(c):
        rows = crawl_people(c, not args.ignore_robots, verify_smtp=verify)
        with lock:
            results.extend(rows)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = [ex.submit(work, c) for c in companies]
        for i, fu in enumerate(as_completed(futs), 1):
            try:
                fu.result()
            except Exception as e:
                print(f"  ! {e}", file=sys.stderr)
            if i % 10 == 0:
                print(f"  …{i}/{len(companies)}", file=sys.stderr)

    known = {(r["company"], r["name"]) for r in results}
    for c, n in seeds:
        if (c.get("name", ""), n) in known:
            continue
        dom = urllib.parse.urlsplit(c["website"]).netloc.lower().removeprefix("www.")
        g = guess_emails(n, dom)
        smtp_status = ""
        if verify and g:
            kept = []
            for cand in g:
                st = smtp_probe(cand)
                if st == "ok":
                    kept = [cand]
                    smtp_status = "ok"
                    break
                if st != "reject":
                    kept.append(cand)
                    smtp_status = smtp_status or st
            g = kept
        results.append({"name": n, "role": "founder (внешний источник)",
                        "email_public": g[0] if smtp_status == "ok" and g else "",
                        "email_guesses": ";".join(g[:4]),
                        "company": c.get("name", ""), "domain": dom,
                        "website": c["website"],
                        "confidence": "high" if smtp_status == "ok" else ("guess" if g else ""),
                        "smtp": smtp_status,
                        "source": c.get("source", "")})

    fields = ["company", "name", "role", "email_public", "email_guesses",
              "confidence", "smtp", "domain", "website", "source"]
    per_company, capped = {}, []
    rank = {"high": 0, "medium": 1, "guess": 2, "": 3}
    for r in sorted(results, key=lambda r: (rank.get(r.get("confidence", ""), 3),
                                            not bool(r.get("name")))):
        c = r.get("company", "")
        if per_company.get(c, 0) >= args.max_per_company:
            continue
        per_company[c] = per_company.get(c, 0) + 1
        capped.append(r)
    results = capped
    results.sort(key=lambda r: ({"high": 0, "medium": 1, "guess": 2}.get(
        r.get("confidence", ""), 3), r.get("company", "").lower()))
    write_csv(args.out, results, fields)
    hi = sum(1 for r in results if r["confidence"] == "high")
    print(f"[people] {len(results)} человек, из них {hi} с опубликованным адресом "
          f"на сайте -> {args.out}", file=sys.stderr)
