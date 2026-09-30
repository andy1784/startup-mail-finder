from __future__ import annotations

import os
import re
import sys
import time
import urllib.parse

import requests

from .constants import UA_API
from .emails import clean_url, normalize_email
from .http import sleep_jitter
from .io import write_csv

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
DEFAULT_FILTERS = [
    '"office"~"^(company|technology|it|coworking|law|consultancy|research)$"']
EXTRA_FILTERS = {
    "craft": '"craft"~"^(software|it|electronics|web|advertising)$"',
    "building": '"building"~"^(office|commercial)$"',
}
STARTUP_NAME_RE = (
    "tech|technolog|softwar|soft|web|digital|data|cloud|ai|machine|robot|"
    "cyber|secur|fintech|startup|solution|system|platform|app|lab|media|"
    "studio|agency|consult|innovat|develop|programm|game|ecommerce|shopify"
)
PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
WIKIDATA = "https://query.wikidata.org/sparql"
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
YC_API = "https://api.ycombinator.com/v0.1/companies"


def overpass_query(city: str, filters: list[str], timeout: int, limit: int,
                   name_re: str = "") -> str:
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
    q = overpass_query(city, filters, timeout, limit, name_re)
    errors = []
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
    return [], False, errors


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
            "source": "osm",
        })
        if len(rows) >= args.limit:
            break

    write_csv(args.out, rows)
    print(f"[osm] {len(rows)} компаний -> {args.out}", file=sys.stderr)


def cmd_places(args):
    key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not key:
        sys.exit("Нужен ключ: export GOOGLE_PLACES_API_KEY=...\n"
                 "(Google даёт бесплатный monthly credit ~$200)")
    rows, seen, page_token = [], set(), None
    for _ in range(args.pages):
        body = {"textQuery": args.query, "pageSize": 20,
                "languageCode": "en", "regionCode": args.region}
        if page_token:
            body["pageToken"] = page_token
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
        page_token = data.get("nextPageToken")
        if not page_token or len(rows) >= args.limit:
            break
        sleep_jitter(1, 2)
    write_csv(args.out, rows[:args.limit])
    print(f"[places] {len(rows[:args.limit])} компаний -> {args.out}", file=sys.stderr)


def get_wikidata(query: str, retries: int = 3) -> list:
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
            if len(rows) >= args.limit:
                break
            site = clean_url(c.get("website") or "")
            if not site:
                continue
            dom = urllib.parse.urlsplit(site).netloc.lower()
            if dom in seen:
                continue
            seen.add(dom)
            locs = c.get("locations") or []
            if locs and isinstance(locs[0], str):
                city = ", ".join(locs[:2])
                country = ""
            else:
                city = ", ".join(filter(None, [l.get("city") for l in locs[:2]]))
                country = ", ".join(filter(None, [l.get("country") for l in locs[:2]]))
            ind = c.get("industries") or []
            if isinstance(ind, dict):
                ind = list(ind.keys())
            tags = c.get("tags") or []
            if isinstance(tags, dict):
                tags = list(tags.keys())
            rows.append({
                "name": c.get("name", ""), "website": site, "domain": dom,
                "city": city, "country": country,
                "tags": ",".join(str(x) for x in tags),
                "industries": ",".join(str(x) for x in ind),
                "batch": c.get("batch", ""), "team_size": c.get("teamSize", ""),
                "oneliners": c.get("oneLiner", ""), "source": "ycombinator",
            })
    write_csv(args.out, rows[:args.limit])
    print(f"[yc] {min(len(rows), args.limit)} компаний -> {args.out}", file=sys.stderr)
