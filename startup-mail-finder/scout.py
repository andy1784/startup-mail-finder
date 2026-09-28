#!/usr/bin/env python3
"""
scout.py — бесплатный поиск стартапов и публичных email-адресов компаний.

Пайплайн:
  1) источник компаний:  osm   (OpenStreetMap Overpass, бесплатно, без ключей)
                          places (Google Places API, нужна карта, есть free tier)
  2) обход сайта компании -> извлечение email
  3) проверка MX-домена + приоритизация ролевых адресов -> CSV для рассылки

Только публичные деловые контакты, опубликованные самими компаниями.
Уважает robots.txt (по умолчанию) и лимитирует скорость.

Использование:
  python3 scout.py osm --city "Berlin" --limit 80 -o companies.csv
  python3 scout.py places --query "startup" --region DE -o companies.csv
  python3 scout.py emails -i companies.csv -o leads.csv --workers 6
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sys
import threading
import time
import urllib.parse
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

requests.packages.urllib3.disable_warnings()  # verify=False — осознанный фолбэк

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0 Safari/537.36 +contact-via-site"
)
# Для Overpass нужен честный бот-UA: их Apache отвечает 406 на строки,
# мимикрирующие под браузер (проверено: curl с Mozilla/5.0 -> 406,
# с обычным бот-UA -> 200).
UA_API = "startup-mail-scout/1.0 (OSM Overpass client; python-requests)"
TIMEOUT = 9
MAX_PAGES_PER_SITE = 6
MAX_QUEUE = 12  # больше кандидатов в очередь не кладём — иначе обход растянется

# Страницы, где адрес почти наверняка есть
CONTACT_PATHS = [
    "/contact", "/contact/", "/kontakt", "/about", "/about-us", "/company",
    "/team", "/careers", "/jobs", "/impressum", "/about/contact", "/hello",
]
CONTACT_HINTS = ("contact", "kontakt", "about", "career", "job", "team",
                 "company", "impressum", "press", "join")

# Ролевые ящики — приоритетные для отклика по вакансии (100 = HR, 60 = общий)
ROLE_PRIORITY = [
    (re.compile(r"^(hr|jobs|recruit\w*|talent|people|hiring|careers?|bewerbung|"
                r"jobsuche|personal|recruitment|join)@", re.I), 100),
    (re.compile(r"\b(hr|recruit|talent|hiring)\b", re.I), 100),
    (re.compile(r"^(hello|hallo|hi|info|contact|kontakt|office|team|mail|mailbox|"
                r"enquiries|inquiries|sekretariat|welcome|willkommen|sales|"
                r"press|pr|partners|hello-world)@", re.I), 60),
]
BAD = re.compile(
    r"^(no-?reply|noreply|donotreply|mailer-daemon|postmaster|abuse|"
    r"webmaster|root|@|\.)|example\.(com|org|net)|sentry|wixpress|"
    r"@2x|sentry\.io|godaddy|domain\.com", re.I)

EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,24}")
FILE_SUFFIX_RE = re.compile(
    r"\.(png|jpe?g|gif|svg|webp|css|js|ico|woff2?|ttf|eot|pdf|zip|mp4)$", re.I)

# mailto obfuscations: "name (at) domain (dot) com", "name [at] domain [dot] com"
DEOBF = [
    (re.compile(r"\s*[\(\[\{]\s*(?:at|@)\s*[\)\]\}]\s*", re.I), "@"),
    (re.compile(r"\s*(?:\[dot\]|\(dot\)|\{dot\}|\sdot\s)\s*", re.I), "."),
    (re.compile(r"\s+(?:at|@)\s+", re.I), "@"),
    (re.compile(r"^mailto:", re.I), ""),
]


# ──────────────────────────── utils ────────────────────────────

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
    return f"{p.scheme}://{p.netloc}"  # origin only


def sleep_jitter(lo=0.8, hi=1.8):
    time.sleep(random.uniform(lo, hi))


def robots_allows(url: str) -> bool:
    """robots.txt читаем один раз на домен и кэшируем: urllib внутри
    RobotFileParser не имеет таймаута и легко вешает обход на десятки секунд."""
    p = urllib.parse.urlsplit(url)
    origin = f"{p.scheme}://{p.netloc}"
    if origin not in _ROBOTS_CACHE:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(origin + "/robots.txt")
        try:
            r = get(origin + "/robots.txt")
            if r.status_code == 200 and len(r.text) < 200_000:
                rp.parse(r.text.splitlines())
            else:
                rp.allow_all = True  # нет robots.txt -> считаем разрешённым
        except Exception:
            rp.allow_all = True
        _ROBOTS_CACHE[origin] = rp
    rp = _ROBOTS_CACHE[origin]
    try:
        return rp.can_fetch(UA, url)
    except Exception:
        return True


_ROBOTS_CACHE: dict[str, object] = {}


_sessions = threading.local()


def session() -> requests.Session:
    """Своя Session на поток: Session не потокобезопасен, плюс так не течёт
    keep-alive-соединение между разными сайтами."""
    s = getattr(_sessions, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept-Language": "en,de;q=0.8"})
        _sessions.s = s
    return s


def get(url: str):
    """GET с фолбэком на verify=False: у малых стартапов часто кривой
    сертификат (неполная цепочка), и весь обход падал бы на первом же таком."""
    try:
        return session().get(url, timeout=TIMEOUT)
    except requests.exceptions.SSLError:
        return session().get(url, timeout=TIMEOUT, verify=False)


def origin_variants(origin: str) -> list[str]:
    """У OSM часто записан несуществующий www или битый протокол.
    Пробуем варианты по убыванию вероятности."""
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


def deobfuscate(text: str) -> str:
    for pat, rep in DEOBF:
        text = pat.sub(rep, text)
    return text


def normalize_email(raw: str) -> str | None:
    e = deobfuscate(raw).strip().strip(".,;:!?\"'()[]<>").lower()
    e = re.sub(r"^mailto:", "", e)
    if not EMAIL_RE.fullmatch(e):
        return None
    if FILE_SUFFIX_RE.search(e) or BAD.search(e) or BAD_ROLEBOX.match(e):
        return None
    local, _, dom = e.partition("@")
    if not local or dom.count(".") == 0 or len(dom) < 4:
        return None
    return e


def role_score(email: str) -> int:
    for pat, score in ROLE_PRIORITY:
        if pat.search(email):
            return score
    return 30


# Домены, с которых пишут стартап-фаундеры лично (тогда сторонний домен — норм)
FREEMAIL = ("gmail.", "googlemail.", "outlook.", "hotmail.", "yahoo.", "icloud.",
            "hey.com", "proton.me", "protonmail.", "fastmail.", "aol.", "me.com")


def email_score(email: str, origin: str) -> int:
    """Приоритет: свой домен > freemail (почта фаундера) > чужой домен.
    Адрес на стороннем домене — это обычно агентство или мусор из JS-бандла."""
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


def has_mx(domain: str) -> bool | None:
    try:
        import dns.resolver
        for t in ("MX", "A"):
            try:
                if dns.resolver.resolve(domain, t, lifetime=4):
                    return True
            except Exception:
                continue
        return False
    except ImportError:
        return None


# ─────────────────────── источник 1: OpenStreetMap ───────────────────────

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
# Готовые OSM-фильтры (каждый — отдельное выражение; их НЕльзя соединять через |:
# конструкция ["a"~"x"|"b"~"y"] в Overpass даёт parse error).
# Важно: building~"^(office|commercial)$" по большому городу — миллионы объектов
# и Overpass отдаёт 504. Наличие ["website"] урежает выдачу в разы.
DEFAULT_FILTERS = [
    '"office"~"^(company|technology|it|coworking|law|consultancy|research)$"']
EXTRA_FILTERS = {
    "craft": '"craft"~"^(software|it|electronics|web|advertising)$"',
    "building": '"building"~"^(office|commercial)$"',  # тяжёлый, только малые города
}
# Теги, по которым компания похожа на стартап, а не на магазин/аптеку
STARTUP_NAME_RE = (
    "tech|technolog|softwar|soft|web|digital|data|cloud|ai|machine|robot|"
    "cyber|secur|fintech|startup|solution|system|platform|app|lab|media|"
    "studio|agency|consult|innovat|develop|programm|game|ecommerce|shopify"
)


def overpass_query(city: str, filters: list[str], timeout: int, limit: int,
                  name_re: str = "") -> str:
    # Фильтр по имени применяем ко всем веткам: office=company в OSM забит
    # магазинами, а нужен tech/soft — иначе выдача бесполезна.
    nf = f'["name"~"{name_re}"]' if name_re else ""
    branches = "\n  ".join(f'nwr(area.a)[{f}]{nf}["website"];' for f in filters)
    return f"""
[out:json][timeout:{timeout}];
area["name"="{city}"]["boundary"="administrative"]->.a;
(
  {branches}
);
out center {limit};
"""


def overpass_fetch(city, filters, name_re, timeout, limit):
    """Запрос к Overpass с повторами на том же зеркале. 504 — это перегрузка
    инстанса, а не ошибка запроса, поэтому пробуем ещё раз там же.
    Возвращает (elements, got_response)."""
    q = overpass_query(city, filters, timeout, limit, name_re)
    els, errors, got = [], [], False
    for url, attempts in zip(OVERPASS_MIRRORS, (2, 1, 1)):
        host = url.split("/")[2]
        for attempt in range(attempts):
            try:
                r = requests.post(url, data={"data": q}, timeout=timeout + 45,
                                  headers={"User-Agent": UA_API,
                                           "Accept": "*/*",
                                           "Content-Type": "application/x-www-form-urlencoded"})
                if r.status_code == 200:
                    return r.json().get("elements", []), True, errors
                errors.append(f"{host}: HTTP {r.status_code}")
            except Exception as e:
                errors.append(f"{host}: {type(e).__name__}")
            if attempt + 1 < attempts:
                time.sleep(3 + attempt * 4)
        if got:
            break
    return els, got, errors


def cmd_osm(args):
    if args.tag:
        filters = [args.tag]
    else:
        filters = list(DEFAULT_FILTERS)
        for key in (args.include or "").split(","):
            if key.strip() in EXTRA_FILTERS:
                filters.append(EXTRA_FILTERS[key.strip()])
    name_re = args.name_filter or (STARTUP_NAME_RE if args.only_startups else "")
    print(f"[osm] Overpass: {args.city} …", file=sys.stderr)
    els, got, errors = overpass_fetch(args.city, filters, name_re,
                                      args.timeout, args.limit * 3)
    if not got:
        sys.exit("[osm] Overpass не ответил (" + "; ".join(errors) + "). "
                 "Ужесточи фильтр (--tag '\"office\"=\"company\"') "
                 "или возьми другой город")
    # 0 совпадений: в малых городах OSM скудно. Автоматически пробуем шире —
    # без фильтра по имени и с дополнительными тегами.
    if not els and not args.tag and name_re:
        print("[osm] 0 совпадений, пробую шире (без фильтра по имени, +craft) …",
              file=sys.stderr)
        wide = list(DEFAULT_FILTERS) + [EXTRA_FILTERS["craft"]]
        els2, got2, _ = overpass_fetch(args.city, wide, "", args.timeout,
                                       args.limit * 3)
        if got2 and els2:
            els, name_re = els2, ""
    if not els:
        sys.exit("[osm] Ответ получен, но 0 совпадений. В этом городе OSM почти "
                 "пуст по компаниям — попробуй крупный город (Berlin, Tallinn, "
                 "Warsaw, Amsterdam) или Places API")

    rows, seen = [], set()
    for el in els:
        t = el.get("tags") or {}
        site = (t.get("website") or t.get("contact:website")
                or t.get("url") or t.get("contact:url") or "")
        site = clean_url(site)
        name = t.get("name") or t.get("brand") or t.get("operator")
        if not site or not name:
            continue
        dom = urllib.parse.urlsplit(site).netloc.lower()
        if dom in seen:
            continue
        # оставляем "похожие на стартап" по ключам, если фильтр включён
        if args.filter and not re.search(
                args.filter, f"{name} {t.get('office') or ''} {t.get('craft') or ''} {t.get('description','')}",
                re.I):
            continue
        seen.add(dom)
        email = normalize_email(t.get("email") or t.get("contact:email") or "")
        phone = t.get("phone") or t.get("contact:phone") or ""
        rows.append({
            "name": name, "website": site, "domain": dom,
            "osm_email": email or "", "phone": phone,
            "city": args.city, "address": t.get("addr:street", ""),
            "tags": ",".join(sorted(k for k in t
                                     if k.startswith(("office", "craft", "building", "industry")))),
        })
        if len(rows) >= args.limit:
            break

    write_csv(args.out, rows)
    print(f"[osm] {len(rows)} компаний -> {args.out}", file=sys.stderr)


# ──────────────────────── источник 2: Google Places ───────────────────────

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"


def cmd_places(args):
    key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not key:
        sys.exit("Нужен ключ: export GOOGLE_PLACES_API_KEY=...\n"
                 "(Google даёт бесплатный monthly credit ~$200)")
    rows, seen = [], set()
    for page in range(args.pages):
        body = {"textQuery": args.query, "pageSize": 20,
                "languageCode": "en", "regionCode": args.region}
        if page:
            body["pageToken"] = None
        r = requests.post(PLACES_URL, json=body, timeout=30, headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": key, "X-Goog-FieldMask":
            "places.id,places.displayName,places.websiteUri,places.formattedAddress,"
            "places.nationalPhoneNumber,places.primaryType,places.rating,"
            "places.userRatingCount,nextPageToken"})
        if r.status_code != 200:
            sys.exit(f"Places API error {r.status_code}: {r.text[:300]}")
        data = r.json()
        for p in data.get("places", []):
            site = clean_url(p.get("websiteUri", ""))
            name = p.get("displayName", {}).get("text", "")
            if not site or not name:
                continue
            dom = urllib.parse.urlsplit(site).netloc.lower()
            if dom in seen:
                continue
            seen.add(dom)
            rows.append({
                "name": name, "website": site, "domain": dom,
                "phone": p.get("nationalPhoneNumber", ""),
                "address": p.get("formattedAddress", ""),
                "type": p.get("primaryType", ""),
                "rating": p.get("rating", ""),
                "reviews": p.get("userRatingCount", ""),
                "source": "google_places",
            })
        sleep_jitter(1, 2)
        nxt = data.get("nextPageToken")
        if not nxt or len(rows) >= args.limit:
            break
        body["pageToken"] = nxt
    write_csv(args.out, rows[:args.limit])
    print(f"[places] {len(rows[:args.limit])} компаний -> {args.out}", file=sys.stderr)


# ────────────────── источник 3: Wikidata (основатели) ──────────────────
# Wikidata — официальный открытый реестр знаний, SPARQL-эндпоинт бесплатный.
# Отдаёт основателей/CEO + официальный сайт компании. Это легальная замена
# LinkedIn для поиска конкретных людей.
WIKIDATA = "https://query.wikidata.org/sparql"
# Q7397 = software industry. Без этого фильтра Wikidata выдаёт пивные гиганты
# (InBev, Heineken), потому что Q4830453 — это «любая коммерческая фирма».
# Фигурные скобки — часть синтаксиса SPARQL, поэтому шаблон собирается
# конкатенацией, а не через str.format.
WIKIDATA_QUERY = """
SELECT ?co ?coLabel ?site ?hqLabel ?founder ?founderLabel ?founderRole
       ?industryLabel ?countryLabel ?employees WHERE {
  ?co wdt:P31/wdt:P279* wd:Q4830453 .
  INDUSTRYPLACEHOLDER
  COUNTRYPLACEHOLDER
  { ?co wdt:P112 ?founder . BIND("founder" AS ?founderRole) }  UNION
  { ?co wdt:P169 ?founder . BIND("CEO" AS ?founderRole) }
  ?founder wdt:P31 wd:Q5 .
  OPTIONAL { ?co wdt:P856 ?site . }
  OPTIONAL { ?co wdt:P159 ?hq . }
  OPTIONAL { ?co wdt:P452 ?industry . }
  OPTIONAL { ?co wdt:P17 ?country . }
  OPTIONAL { ?co wdt:P1128 ?employees . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "LANGPLACEHOLDER". }
}
LIMIT LIMITPLACEHOLDER
"""
# ISO-коды стран -> QID Wikidata (только те, что встречаются в стартап-мире)
WIKIDATA_COUNTRIES = {
    "de": "Q183", "germany": "Q183", "deutschland": "Q183",
    "gb": "Q145", "uk": "Q145", "united kingdom": "Q145", "england": "Q145",
    "us": "Q30", "usa": "Q30", "united states": "Q30",
    "nl": "Q55", "netherlands": "Q55",
    "fr": "Q142", "france": "Q142",
    "ee": "Q191", "estonia": "Q191",
    "il": "Q801", "israel": "Q801",
    "se": "Q34", "sweden": "Q34",
    "ch": "Q39", "switzerland": "Q39",
    "es": "Q29", "spain": "Q29",
    "pl": "Q36", "poland": "Q36",
    "ua": "Q212", "ukraine": "Q212",
    "ca": "Q16", "canada": "Q16",
    "au": "Q408", "australia": "Q408",
    "ie": "Q27", "ireland": "Q27",
    "fi": "Q33", "finland": "Q33",
    "dk": "Q35", "denmark": "Q35",
    "cz": "Q213", "czechia": "Q213",
    "at": "Q40", "austria": "Q40",
    "pt": "Q45", "portugal": "Q45",
    "sg": "Q334", "singapore": "Q334",
    "lt": "Q37", "lithuania": "Q37",
    "lv": "Q211", "latvia": "Q211",
}


def cmd_wikidata(args):
    industry = ("?co wdt:P452 ?ind . ?ind wdt:P279* wd:Q7397 ."
                if args.tech_only else "")
    country = ""
    qids = []
    for c in (args.countries or "").split(","):
        c = c.strip().lower()
        if not c:
            continue
        qid = WIKIDATA_COUNTRIES.get(c)
        if not qid:
            sys.exit(f"[wikidata] неизвестная страна '{c}'. Доступны: "
                     + ", ".join(sorted(set(WIKIDATA_COUNTRIES))))
        qids.append(qid)
    if qids:
        # Именно VALUES, а не цепочка «?co wdt:P17 wd:Q183 . ?co wdt:P17 wd:Q191 .»:
        # цепочка требует, чтобы компания была во всех странах сразу (=> пусто).
        country = (f"?co wdt:P17 ?countryFilter . VALUES ?countryFilter {{ "
                   + " ".join("wd:" + q for q in qids) + " }")
    q = (WIKIDATA_QUERY.replace("INDUSTRYPLACEHOLDER", industry)
                          .replace("COUNTRYPLACEHOLDER", country)
                          .replace("LANGPLACEHOLDER", args.lang)
                          .replace("LIMITPLACEHOLDER", str(args.limit)))
    print("[wikidata] SPARQL …", file=sys.stderr)
    r = get_wikidata(q)
    rows, seen = [], set()
    for b in r:
        d = {k: (v.get("value") if isinstance(v, dict) else v)
             for k, v in b.items()}
        site = clean_url(d.get("site", ""))
        name = d.get("coLabel", "")
        founder = d.get("founderLabel", "")
        if not site or not founder:
            continue
        # безымянные сущности приходят как Q-идентификаторы — такое имя не годится
        if re.fullmatch(r"Q\d+", founder.strip()) or re.fullmatch(r"Q\d+", name.strip()):
            continue
        key = (d.get("co", ""), founder)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "name": name, "website": site,
            "domain": urllib.parse.urlsplit(site).netloc,
            "founder": founder, "founder_role": d.get("founderRole", ""),
            "hq": d.get("hqLabel", ""), "industry": d.get("industryLabel", ""),
            "country": d.get("countryLabel", ""), "employees": d.get("employees", ""),
            "source": "wikidata",
        })
    write_csv(args.out, rows)
    print(f"[wikidata] {len(rows)} связок компания→основатель -> {args.out}",
          file=sys.stderr)


def get_wikidata(query: str, retries: int = 3) -> list:
    """SPARQL-эндпоинт часто отдаёт 429/503: нужен честный User-Agent с контактом
    и повтор с паузой (эндпоинт явно требует этого в своих правилах)."""
    for attempt in range(retries):
        try:
            r = requests.get(WIKIDATA, params={"query": query, "format": "json"},
                             headers={"User-Agent": UA_API, "Accept": "application/sparql-results+json"},
                             timeout=90)
            if r.status_code == 200:
                return r.json()["results"]["bindings"]
            if r.status_code in (429, 503, 502):
                time.sleep(5 + attempt * 8)
                continue
            sys.exit(f"[wikidata] HTTP {r.status_code}: {r.text[:200]}")
        except requests.exceptions.RequestException as e:
            if attempt + 1 < retries:
                time.sleep(5)
                continue
            sys.exit(f"[wikidata] сеть: {e}")
    sys.exit("[wikidata] эндпоинт не ответил после " + str(retries) + " попыток")


# ──────────────────────── источник 4: YC directory ───────────────────────
# Официальное публичное API Y Combinator: бесплатно, с ключом и без.
YC_API = "https://api.ycombinator.com/v0.1/companies"


def cmd_yc(args):
    print("[yc] Y Combinator API …", file=sys.stderr)
    headers = {"User-Agent": UA_API}
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"
    rows, seen, page = [], set(), 0
    while len(rows) < args.limit and page < args.max_pages:
        params = {"per_page": 100, "page": page + 1}
        if args.batch:
            params["batch"] = args.batch
        r = requests.get(YC_API, params=params, headers=headers, timeout=30)
        if r.status_code != 200:
            sys.exit(f"[yc] HTTP {r.status_code}: {r.text[:200]}")
        batch = r.json().get("companies", [])
        if not batch:
            break
        page += 1
        for c in batch:
            site = clean_url(c.get("website") or "")
            if not site:
                continue
            dom = urllib.parse.urlsplit(site).netloc.lower()
            if dom in seen:
                continue
            seen.add(dom)
            locs = c.get("locations") or []
            # API отдаёт locations и industries как списки строк (проверено)
            if locs and isinstance(locs[0], str):
                city = ", ".join(locs[:2])
                country = ""
            else:
                city = ", ".join(filter(None, [l.get("city") for l in locs[:2]]))
                country = ", ".join(filter(None, [l.get("country") for l in locs[:2]]))
            ind = c.get("industries") or []
            if isinstance(ind, dict):
                ind = list(ind.keys())
            rows.append({
                "name": c.get("name", ""), "website": site, "domain": dom,
                "city": city, "country": country,
                "tags": ",".join(c.get("tags") or []),
                "industries": ",".join(ind),
                "batch": c.get("batch", ""), "team_size": c.get("teamSize", ""),
                "oneliners": c.get("oneLiner", ""), "source": "ycombinator",
            })
    write_csv(args.out, rows[:args.limit])
    print(f"[yc] {min(len(rows), args.limit)} компаний -> {args.out}", file=sys.stderr)


# ───────────────────── источник 5: Impressum / разметка ─────────────────────
# Немецкий закон обязывает в Impressum называть Geschäftsführer. Это самый
# чистый легальный источник имен первых лиц компаний в DE/AT/CH.
PERSON_LABELS = re.compile(
    r"(Geschäftsführ(?:er|erin)|Managing Director|CEO|Chief Executive|"
    r"Co-?Founder|Founder|Mitgründer(?:in)?|Inhaber(?:in)?|Owner|Verantwortlich|"
    r"Director|Geschäftsführung|Unternehmensführung|Managing Partner|"
    r"Vertretungsberechtigt)", re.I)
# Слова-разделители после имени: конец блока в Impressum
# ВАЖНО: у всех вариантов обязательны \b с обеих сторон, иначе "Web" совпадает
# внутри имени "Weber", а "Mail" — внутри "Email" (проверено на реальном тексте).
NAME_STOP = re.compile(
    r"\b(?:HRB|HR-Nr|HRA|AGB|Amtsgericht|Registergericht|USt-IdNr|USt|"
    r"Telefon|Tel|Fax|Mail|Email|E-Mail|Web|Website|Register|Portal|Sitz|"
    r"Anschrift|Vertretungsberechtigt|NBank|Kontonummer|BIC|IBAN|"
    r"Versicherungsnummer|Geschäftsführ\w*|Managing\s+Director|Founder|"
    r"Inhaber\w*|Managing\s+Partner|CEO|Mitgründer\w*|Co-?Founder|"
    r"Steuernummer|Mitarbeiter\w*|Team|Beschäftigte)\b|&", re.I)
TITLE_PREFIX = re.compile(r"^(Dr\.?(-Ing\.?)?|Prof\.?|Dr\.?h\.c\.?|Ing\.?|"
                          r"Dipl\.?|B\.?Sc\.?|M\.?Sc\.?|MBA|MSc|PhD|Phd|"
                          r"Dr|Prof)\.?\s+", re.I)
STOPWORDS = {
    # немецкие служебные слова — без них «Wir sind bemüht uns» проходил как имя
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
    # английские служебные слова и подписи ссылок
    "the", "and", "or", "of", "for", "with", "our", "we", "are", "is", "be",
    "to", "in", "on", "at", "this", "that", "these", "those", "not", "all",
    "any", "can", "may", "write", "here", "click", "mail", "email", "e",
    "contact", "us", "imprint", "privacy", "legal", "more", "about", "team",
    "company", "page", "phone", "tel", "fax", "ceo", "founder",
}
# Мусорные ящики: для отклика на вакансию бесполезны
BAD_ROLEBOX = re.compile(r"^(datenschutz|privacy|impressum|legal|abuse|dpo|"
                        r"gdpr|schadensmeldung|presse|redaktion|newsroom|"
                        r"postmaster|webmaster|hostmaster)@", re.I)


# Глаголы-призывы в подписях ссылок: «E-Mail schreiben», «write us», «click here»
ACTION_WORDS = {"schreiben", "write", "click", "klick", "hier", "here", "jetzt",
                "now", "kontakt", "contact", "aufrufen", "open", "ansehen",
                "view", "mehr", "more", "send", "senden", "mailen"}
# Префиксы в aria-label/title: «Email Maaz Sheikh» -> «Maaz Sheikh»
LABEL_PREFIX = re.compile(r"^(?:e-?mail|email|mail|write (?:an )?to|contact)\b[\s:]*",
                          re.I)


def _garbage_name(label: str) -> bool:
    """«E-Mail schreiben», «write us», «click here» — это подпись ссылки,
    а не имя человека."""
    if not label:
        return True
    toks = [t.strip(".,&!?").lower() for t in label.split() if t.strip(".,&!?")]
    if not toks:
        return True
    if any(t in ACTION_WORDS for t in toks):
        return True
    # разбиваем составные слова: «e-mail» -> e, mail
    flat = [p for t in toks for p in re.split(r"[^a-zäöüß]+", t) if p]
    return all(t in STOPWORDS for t in toks) or all(t in STOPWORDS for t in flat)


def _looks_like_person(tokens: list[str]) -> bool:
    """2-4 слова, первое с заглавной, нет служебных/наименований компании."""
    if not 2 <= len(tokens) <= 4:
        return False
    if any(t.lower().strip(".,") in STOPWORDS for t in tokens):
        return False
    if any(len(t) > 22 for t in tokens):
        return False
    return bool(re.match(r"^[A-ZÄÖÜ][a-zäöüßA-Z-]{1,}$", tokens[0]))


def extract_people_from_text(text: str) -> list[tuple[str, str]]:
    """Ищет 'Geschäftsführer: Иван Петров' и подобное -> [(имя, роль)]."""
    out = []
    for m in PERSON_LABELS.finditer(text):
        role = m.group(0)
        tail = text[m.end():m.end() + 160]
        tail = tail.lstrip(" :\t–—-")
        # хвост часто начинается со связки: «owner is James», «founder who is X»
        tail = re.sub(r"^(?:is|ist|sind|was|wer|who|that|namely|namely)\b[\s:]*",
                      "", tail, flags=re.I)
        tail = tail.lstrip(" :\t–—-")
        # режем по разделителю блока Impressum
        stop = NAME_STOP.search(tail)
        if stop:
            tail = tail[:stop.start()]
        tail = tail.split("\n")[0]
        # «founder and CEO is Alex R.»: берём первый кусок, который похож на
        # человека, а не буквально первый (там может стоять «CEO» или «is»)
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
    # один и тот же человек может встретиться дважды (Impressum дублирует блок)
    seen, uniq = set(), []
    for name, role in out:
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        uniq.append((name, role))
    return uniq


def extract_people_from_ldjson(soup) -> list[tuple[str, str]]:
    """JSON-LD Organization: founder / employee с именем и иногда email."""
    out = []
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "{}")
        except Exception:
            continue
        for node in json.dumps(data).split('"@type"')[1:]:
            try:
                obj = json.loads("{" + '"@type"' + node.split("},")[0] + "}")
            except Exception:
                continue
            name = obj.get("name", "")
            if isinstance(name, dict):
                name = name.get("@value", "") or name.get("name", "")
            if not isinstance(name, str) or "@" in name:
                continue
            m = EMAIL_RE.search(str(obj.get("email", "")))
            if m:
                e = normalize_email(m.group(0))
                if e:
                    out.append((e, name))
    return out


# ──────────────────── этап 2: обход сайта за email ────────────────────

def soup_of(html: str):
    return BeautifulSoup(html, "html.parser")


def harvest_emails_from_html(html: str) -> set[str]:
    """Ищем адреса тремя проходами: mailto-ссылки, видимый текст и сырой HTML.
    Третий проход обязателен — письма часто лежат в атрибутах, alt-тегах,
    JSON-LD Organization или собранном JS-бандле, а не в текстовых узлах."""
    found = set()
    soup = soup_of(html)
    for a in soup.find_all("a", href=True):
        if a["href"].lower().startswith("mailto:"):
            e = normalize_email(a["href"])
            if e:
                found.add(e)
    # видимый текст, включая скрипты
    for m in EMAIL_RE.findall(deobfuscate(soup.get_text(" "))):
        e = normalize_email(m)
        if e:
            found.add(e)
    # сырой HTML: атрибуты, JSON-LD, JS
    for m in EMAIL_RE.findall(deobfuscate(html)):
        e = normalize_email(m)
        if e:
            found.add(e)
    return found


def candidate_urls(origin: str, soup) -> list[str]:
    urls = []
    for a in soup.find_all("a", href=True):
        href = urllib.parse.urljoin(origin + "/", a["href"])
        p = urllib.parse.urlsplit(href)
        if f"{p.scheme}://{p.netloc}" != origin:
            continue
        path = p.path.lower().rstrip("/")
        if any(h in path for h in CONTACT_HINTS) and path:
            urls.append(href.split("#")[0])
    for p_ in CONTACT_PATHS:
        urls.append(origin + p_)
    # дедуп по пути, сохраняя порядок
    seen, out = set(), []
    for u in urls:
        k = urllib.parse.urlsplit(u).path.rstrip("/") or "/"
        if k not in seen:
            seen.add(k)
            out.append(u)
    return urls[:MAX_QUEUE]


CHROME_CANDIDATES = ["/usr/bin/chromium", "/usr/bin/chromium-browser",
                      "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"]
_CHROME_SLOTS = threading.Semaphore(2)  # не больше 2 браузеров сразу: они жрют ~300 МБ


def render_with_js(url: str, budget_ms=6000) -> str | None:
    """Для SPA-сайтов (React/Next/Angular) обычный requests видит пустую страницу.
    Chromium в headless-режиме рендерит JS и отдаёт финальный DOM в stdout
    (--dump-dom), так что драйвер/CDP не нужен. Playwright в системе может быть
    битым, поэтому идём через сам браузер."""
    import shutil
    import subprocess
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
        if r.status_code != 200 or "html" not in r.headers.get("Content-Type", ""):
            continue
        emails |= harvest_emails_from_html(r.text)
        if pages == 1:  # с первой страницы набираем ссылки на контакты
            queue += candidate_urls(origin, soup_of(r.text))
        sleep_jitter(0.4, 1.0)

    js_used = ""
    # Браузер нужен, когда обычный обход ничего не дал. Проверка по размеру HTML
    # была ложной: SPA-оболочка бывает и 12 КБ (ratiodata.com), и рендерится в 155 КБ.
    if not emails and js and pages >= 0:
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
    out["email_kind"] = ("hr" if top >= 100 else      # hr@, jobs@, careers@
                         "role" if top >= 60 else      # hello@, info@
                         "named" if scored else "")    # имя.фамилия@
    out["on_site"] = "y" if on_site else "n"
    out["note"] = "" if emails else "no_public_email (проверь /careers или LinkedIn)"
    out["mx"] = "ok" if scored and has_mx(scored[0].split("@")[1]) else ""
    out["pages_crawled"] = pages
    out["js_rendered"] = js_used
    out["website"] = origin  # фактически рабочий домен
    out["source_domain"] = company["website"]
    return out


def cmd_emails(args):
    with open(args.input, newline="", encoding="utf-8") as f:
        companies = list(csv.DictReader(f))
    companies = [c for c in companies if c.get("website")]
    print(f"[emails] обход {len(companies)} сайтов, workers={args.workers}",
          file=sys.stderr)

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(crawl_domain, c, not args.ignore_robots,
                          args.verbose, args.js): c
                for c in companies}
        for i, fu in enumerate(as_completed(futs), 1):
            try:
                results.append(fu.result())
            except Exception as e:
                print(f"  ! {futs[fu].get('name')}: {e}", file=sys.stderr)
            if i % 10 == 0:
                print(f"  …{i}/{len(companies)}", file=sys.stderr)

    # Приоритет наверх: сначала ролевые адреса, потом именные, потом без адреса
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


# ───────────────────── этап 2b: люди + адреса ─────────────────────
# Заменяет LinkedIn: имена первых лиц берём из Impressum/JSON-LD/текста,
# адреса — опубликованные или построенные по корпоративному шаблону.
PEOPLE_PAGES = ["/impressum", "/impressum/", "/about", "/about-us", "/team",
                "/company", "/contact", "/kontakt", "/about/company",
                "/en/about", "/de/impressum", "/en/impressum"]
UMLAUTS = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "Ae", "Ö": "Oe",
           "Ü": "Ue"}


TITLE_RE = re.compile(r"^(Dr|Prof|Ing|Dipl|PhD|Phd|MSc|MBA|BSc|MA)\b", re.I)


def _strip_titles(text: str) -> str:
    """Убирает 'Dr.-Ing. Johannes' -> 'Johannes'. Титулов может быть несколько
    подряд, поэтому повторяем, пока начало меняется."""
    for _ in range(4):
        before = text
        text = TITLE_RE.sub("", text).lstrip(" .-")
        if text == before:
            break
    return text


def slug_name(name: str) -> tuple[str, str] | None:
    """'Dr.-Ing. Johannes Gräbert' -> ('johannes', 'graebert')."""
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
    """Корпоративные шаблоны адресов, отсортированные по частоте в EU/US."""
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


def crawl_people(company: dict, respect_robots=True) -> list[dict]:
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
        if r.status_code != 200 or "html" not in r.headers.get("Content-Type", ""):
            continue
        soup = soup_of(r.text)
        # 1) mailto с именем в тексте ссылки или рядом
        for a in soup.find_all("a", href=True):
            if not a["href"].lower().startswith("mailto:"):
                continue
            e = normalize_email(a["href"])
            if not e:
                continue
            label = a.get_text(" ", strip=True) or a.get("title", "") \
                or a.get("aria-label", "")
            # подпись ссылки — не всегда имя: «E-Mail schreiben», «write us»
            if EMAIL_RE.fullmatch(label or "") or _garbage_name(label):
                label = ""
            if label:
                label = LABEL_PREFIX.sub("", label).strip(" .:;-") or ""
            add(label, "", e, "mailto")
        # 2) JSON-LD
        for e, name in extract_people_from_ldjson(soup):
            add(name, "", e, "ld+json")
        # 3) подписи с именем в тексте (Impressum, About)
        text = re.sub(r"\s+", " ", soup.get_text(" "))
        for name, role in extract_people_from_text(text):
            add(name, role, "", "impressum/about")
        sleep_jitter(0.3, 0.8)

    out = []
    # Один ящик на нескольких людей = общий вход (support@ у ageospatial.com
    # закреплён сразу за двумя). Имени тут не существует — не выдумываем.
    names_per_mail: dict[str, set] = {}
    for rec in found.values():
        if rec.get("name") and rec.get("email_public"):
            names_per_mail.setdefault(rec["email_public"], set()).add(rec["name"])
    shared = {m for m, n in names_per_mail.items() if len(n) > 1}

    for key, rec in list(found.items()):
        rec = dict(rec)
        if rec.get("email_public") in shared:
            rec["name"] = ""
            rec["role"] = "общий ящик на нескольких сотрудников"
        rec["company"] = company.get("name", "")
        rec["domain"] = dom
        rec["website"] = origin
        guesses = [g for g in guess_emails(rec["name"], dom)
                   if g != rec["email_public"]]
        rec["email_guesses"] = ";".join(guesses[:4])
        if rec["email_public"] and rec["email_public"].endswith("@" + dom):
            rec["confidence"] = "high"        # опубликован на сайте компании
        elif rec["email_public"]:
            rec["confidence"] = "medium"      # опубликован, но чужой домен
        elif guesses:
            rec["confidence"] = "guess"       # построен по шаблону, не проверен
        else:
            rec["confidence"] = ""
        out.append(rec)

    # после обнуления имён у общих ящиков могли появиться дубли
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
    # компания может прийти с уже известным основателем (из wikidata)
    seeds = []
    for c in companies:
        for extra in (c.get("founder", ""), c.get("owner", "")):
            for n in re.split(r"[,;/]| and ", extra or ""):
                n = n.strip()
                if n:
                    seeds.append((c, n))
    print(f"[people] {len(companies)} сайтов"
          + (f" + {len(seeds)} имён из источника" if seeds else ""),
          file=sys.stderr)

    results, lock = [], threading.Lock()

    def work(c):
        rows = crawl_people(c, not args.ignore_robots)
        with lock:
            results.extend(rows)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(work, c) for c in companies]
        for i, fu in enumerate(as_completed(futs), 1):
            try:
                fu.result()
            except Exception as e:
                print(f"  ! {e}", file=sys.stderr)
            if i % 10 == 0:
                print(f"  …{i}/{len(companies)}", file=sys.stderr)

    # добавляем людей, которых дал внешний источник (wikidata)
    known = {(r["company"], r["name"]) for r in results}
    for c, n in seeds:
        if (c.get("name", ""), n) in known:
            continue
        dom = urllib.parse.urlsplit(c["website"]).netloc.lower().removeprefix("www.")
        g = guess_emails(n, dom)
        results.append({"name": n, "role": "founder (внешний источник)",
                        "email_public": "", "email_guesses": ";".join(g[:4]),
                        "company": c.get("name", ""), "domain": dom,
                        "website": c["website"],
                        "confidence": "guess" if g else "",
                        "source": c.get("source", "")})

    fields = ["company", "name", "role", "email_public", "email_guesses",
              "confidence", "domain", "website", "source"]
    # у одной компании бывает десяток ящиков (berlin-team@, berlin-pcs@, …) —
    # в выдаче это шум, оставляем несколько самых полезных
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


# ──────────────────── этап 3: черновики писем ────────────────────

DEFAULT_TEMPLATE = """Subject: Bewerbung — {role} ({your_name})

Guten Tag {team},

ich heiße {your_name} und bewerbe mich um die Position {role} bei {company}.

{why_us}

Mein Lebenslauf liegt diesem Schreiben bei. Für Rückfragen bin ich unter
{your_email} erreichbar.

Mit freundlichen Grüßen
{your_name}
{phone}

--
Diese Nachricht wurde einmalig manuell verfasst und nicht automatisiert versendet.
Wenn Sie keine weiteren Nachrichten wünschen, melden Sie sich bitte kurz.
"""

def cmd_drafts(args):
    """Черновики писем. Если передан people.csv — адресуем конкретному человеку
    (person mode), иначе берём лучший адрес компании из leads.csv."""
    people = []
    if args.people:
        with open(args.people, newline="", encoding="utf-8") as f:
            people = [r for r in csv.DictReader(f) if r.get("email_public")
                      or r.get("email_guesses")]
    if people:
        rows_in = [{"email": p.get("email_public") or (p.get("email_guesses", "")
                                                        .split(";") or [""])[0],
                    "name": p.get("company", ""), "person": p.get("name", ""),
                    "role": p.get("role", ""),
                    "confidence": p.get("confidence", ""),
                    "website": p.get("website", ""), "domain": p.get("domain", "")}
                   for p in people]
    else:
        with open(args.input, newline="", encoding="utf-8") as f:
            rows_in = [r for r in csv.DictReader(f) if r.get("email")]

    tpl = DEFAULT_TEMPLATE
    if args.template:
        tpl = open(args.template, encoding="utf-8").read()

    outdir = args.outdir
    os.makedirs(outdir, exist_ok=True)
    rows = []
    for i, r in enumerate(rows_in, 1):
        name = (r.get("name") or "").strip() or r.get("domain", "")
        team = name.split()[0] if name else "Team"
        first = (r.get("person") or "").split()[0] if r.get("person") else ""
        # Если имя известно — обращаемся по имени. «Guten Tag Stephan Sprylab»
        # (имя + первое слово компании) звучит нелепо, а «Herr/Frau» угадывать
        # нельзя: пол в данных не указан.
        addr = first if first else f"Team {team}"
        vals = {
            "company": name, "team": addr, "first_name": first,
            "role": args.role, "why_us": args.why or "",
            "your_name": args.name, "your_email": args.email,
            "phone": args.phone or "", "addr": f"{team} {name}".strip(),
        }
        try:
            body = tpl.format(**vals)
        except KeyError as e:
            sys.exit(f"В шаблоне неизвестная переменная: {e}. "
                     f"Доступны: company, team, addr, first_name, role, why_us, "
                     f"your_name, your_email, phone")
        subj = body.split("\n", 1)[0]
        body = body.split("\n", 1)[1].lstrip("\n")
        suffix = f"_{r['person'].split()[0]}" if r.get("person") else ""
        fn = os.path.join(outdir, f"{i:03d}_{r.get('domain') or name}{suffix}.txt")
        header = f"To: {r['email']}"
        if r.get("confidence") == "guess":
            header += "   # ВНИМАНИЕ: адрес построен по шаблону, не проверен!\n"
        with open(fn, "w", encoding="utf-8") as fh:
            fh.write(f"{header}\n{subj}\n{body}")
        rows.append({"to": r["email"], "person": r.get("person", ""),
                     "confidence": r.get("confidence", ""),
                     "subject": subj[8:].strip(), "draft": fn, "company": name,
                     "website": r.get("website", ""),
                     "mailto": "mailto:" + urllib.parse.quote(r["email"])
                               + "?subject=" + urllib.parse.quote(subj[8:].strip())})
    write_csv(args.out or os.path.join(outdir, "drafts_index.csv"), rows)
    warn = sum(1 for r in rows if r["confidence"] == "guess")
    print(f"[drafts] {len(rows)} черновиков -> {outdir}/", file=sys.stderr)
    if warn:
        print(f"[drafts] {warn} адресов построены по шаблону (confidence=guess). "
              f"Перед отправкой проверь их: неверный адрес хуже, чем общий info@.",
              file=sys.stderr)
    print("[drafts] Отправка — вручную, из своего почтового клиента: так ты "
          "видишь каждое письмо и не ловишь блок за спам.", file=sys.stderr)


# ─────────────────────────────── io ───────────────────────────────

def write_csv(path, rows, fields=None):
    if not rows:
        print("[io] нет данных", file=sys.stderr)
        return
    fields = fields or list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})
    print(f"[io] {len(rows)} строк -> {path}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("osm", help="компании из OpenStreetMap (бесплатно, без ключа)")
    p.add_argument("--city", required=True)
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--filter", default="",
                   help="regex по имени/тегам уже в Python, напр. 'tech|soft'")
    p.add_argument("--only-startups", action="store_true", default=True,
                   help="фильтровать по названию на tech/soft/data (по умолчанию)")
    p.add_argument("--any-company", dest="only_startups", action="store_false",
                   help="не фильтровать: все офисы с сайтом")
    p.add_argument("--name-filter", default="",
                   help="свой regex по имени для Overpass")
    p.add_argument("--tag", default="", help="свой OSM-фильтр вместо дефолтного")
    p.add_argument("--include", default="",
                   help="дополнительные теги через запятую: craft, building")
    p.add_argument("--timeout", type=int, default=45,
                   help="таймаут запроса Overpass, сек (сервер сам оборвёт на этом)")
    p.add_argument("-o", "--out", default="companies.csv")
    p.set_defaults(func=cmd_osm)

    p = sub.add_parser("places", help="компании из Google Places API")
    p.add_argument("--query", default="tech startup")
    p.add_argument("--region", default="DE")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--pages", type=int, default=5)
    p.add_argument("-o", "--out", default="companies.csv")
    p.set_defaults(func=cmd_places)

    p = sub.add_parser("wikidata", help="компании + основатели из Wikidata")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--lang", default="en")
    p.add_argument("--countries", default="",
                   help="через запятую: de,gb,us,ee,il,nl… (пусто = любые)")
    p.add_argument("--tech-only", action="store_true", default=True,
                   help="только софт/IT-компании (иначе прилетят пивные гиганты)")
    p.add_argument("--any-industry", dest="tech_only", action="store_false")
    p.add_argument("-o", "--out", default="companies.csv")
    p.set_defaults(func=cmd_wikidata)

    p = sub.add_parser("yc", help="стартапы из официального API Y Combinator")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--max-pages", type=int, default=5)
    p.add_argument("--batch", default="", help="например Winter/Summer/Spring")
    p.add_argument("--token", default=os.environ.get("YC_API_TOKEN", ""))
    p.add_argument("-o", "--out", default="companies.csv")
    p.set_defaults(func=cmd_yc)

    p = sub.add_parser("people", help="первые лица компаний + их адреса")
    p.add_argument("-i", "--input", default="companies.csv")
    p.add_argument("-o", "--out", default="people.csv")
    p.add_argument("--workers", type=int, default=5)
    p.add_argument("--max-per-company", type=int, default=3,
                   help="сколько адресов оставить от одной компании")
    p.add_argument("--ignore-robots", action="store_true")
    p.set_defaults(func=cmd_people)

    p = sub.add_parser("emails", help="обойти сайты и собрать email")
    p.add_argument("-i", "--input", default="companies.csv")
    p.add_argument("-o", "--out", default="leads.csv")
    p.add_argument("--workers", type=int, default=5)
    p.add_argument("--ignore-robots", action="store_true")
    p.add_argument("--js", action="store_true",
                   help="если обычный обход не нашёл email — отрендерить через headless Chromium")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_emails)

    p = sub.add_parser("drafts", help="черновики писем по найденным адресам")
    p.add_argument("-i", "--input", default="leads.csv")
    p.add_argument("--people", default="",
                   help="people.csv: писать конкретным людям, а не в info@")
    p.add_argument("--name", required=True, help="твоё имя")
    p.add_argument("--email", required=True, help="твой обратный адрес")
    p.add_argument("--role", required=True, help="на кого откликаешься, напр. 'Data Engineer'")
    p.add_argument("--why", default="", help="1-2 предложения, почему именно они")
    p.add_argument("--phone", default="")
    p.add_argument("--template", default="", help="свой шаблон .txt")
    p.add_argument("--outdir", default="outreach")
    p.add_argument("-o", "--out", default="")
    p.set_defaults(func=cmd_drafts)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
