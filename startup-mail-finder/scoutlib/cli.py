from __future__ import annotations

import argparse
import os

from .crawl import cmd_emails
from .drafts import cmd_drafts
from .people import cmd_people
from .pipeline import cmd_merge, cmd_run
from .send import cmd_send
from .sources import cmd_osm, cmd_places, cmd_wikidata, cmd_yc

DOC = """scout — бесплатный поиск стартапов и публичных email-адресов компаний.

Пайплайн:
  1) источник компаний:  osm / places / wikidata / yc
  2) обход сайта компании -> извлечение email
  3) MX + SMTP-проверка guess-адресов -> CSV для рассылки

Использование:
  python3 scout.py osm --city "Berlin" --limit 80 -o companies.csv
  python3 scout.py places --query "startup" --region DE -o companies.csv
  python3 scout.py emails -i companies.csv -o leads.csv --workers 6 --resume
  python3 scout.py merge -i yc.csv osm.csv -o companies.csv
  python3 scout.py run --sources yc,osm --city Berlin --name "Ada" --email a@x.io --role Engineer
  python3 scout.py drafts -i leads.csv --lang en --name "Ada" --email a@x.io --role "Engineer"
"""


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=DOC,
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
                   help="таймаут запроса Overpass, сек")
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
    p.add_argument("--verify-smtp", action="store_true",
                   help="SMTP RCPT TO для guess-адресов (не отправляет письмо)")
    p.add_argument("--resume", action="store_true",
                   help="не обходить компании, уже есть в выходном CSV")
    p.set_defaults(func=cmd_people)

    p = sub.add_parser("emails", help="обойти сайты и собрать email")
    p.add_argument("-i", "--input", default="companies.csv")
    p.add_argument("-o", "--out", default="leads.csv")
    p.add_argument("--workers", type=int, default=5)
    p.add_argument("--ignore-robots", action="store_true")
    p.add_argument("--js", action="store_true",
                   help="если обычный обход не нашёл email — отрендерить через headless Chromium")
    p.add_argument("--resume", action="store_true",
                   help="пропустить домены, уже записанные в -o")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_emails)

    p = sub.add_parser("merge", help="склеить CSV компаний по домену")
    p.add_argument("-i", "--input", nargs="+", default=[], dest="merge_files",
                   help="CSV компаний (можно несколько)")
    p.add_argument("inputs", nargs="*", help="ещё CSV без флага")
    p.add_argument("-o", "--out", default="companies.csv")
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("run", help="полный пайплайн: источники -> emails -> people -> drafts")
    p.add_argument("--sources", default="yc", help="через запятую: yc,osm,wikidata")
    p.add_argument("--city", default="", help="нужен для osm")
    p.add_argument("--countries", default="", help="для wikidata, напр. de,us")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--max-pages", type=int, default=2)
    p.add_argument("--batch", default="")
    p.add_argument("--token", default=os.environ.get("YC_API_TOKEN", ""))
    p.add_argument("--workers", type=int, default=5)
    p.add_argument("--js", action="store_true")
    p.add_argument("--verify-smtp", action="store_true")
    p.add_argument("--workdir", default=".")
    p.add_argument("--name", default="")
    p.add_argument("--email", default="")
    p.add_argument("--role", default="")
    p.add_argument("--why", default="")
    p.add_argument("--phone", default="")
    p.add_argument("--lang", default="en", choices=["en", "de"])
    p.add_argument("--draft-people", action="store_true",
                   help="черновики по people.csv, а не по leads.csv")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("drafts", help="черновики писем по найденным адресам")
    p.add_argument("-i", "--input", default="leads.csv")
    p.add_argument("--people", default="",
                   help="people.csv: писать конкретным людям, а не в info@")
    p.add_argument("--name", required=True, help="твоё имя")
    p.add_argument("--email", required=True, help="твой обратный адрес")
    p.add_argument("--role", required=True, help="на кого откликаешься, напр. 'Data Engineer'")
    p.add_argument("--why", default="", help="1-2 предложения, почему именно они")
    p.add_argument("--phone", default="")
    p.add_argument("--lang", default="en", choices=["en", "de"],
                   help="язык шаблона: en (YC/US) или de (Impressum/DACH)")
    p.add_argument("--template", default="", help="свой шаблон .txt")
    p.add_argument("--outdir", default="outreach")
    p.add_argument("-o", "--out", default="")
    p.set_defaults(func=cmd_drafts)

    p = sub.add_parser("send", help="отправить черновики через твой SMTP (по умолчанию dry-run)")
    p.add_argument("-i", "--input", default="",
                   help="drafts_index.csv (по умолчанию outreach/drafts_index.csv)")
    p.add_argument("--outdir", default="outreach")
    p.add_argument("--name", default="", help="имя в From")
    p.add_argument("--email", default="", help="fallback From, если нет SMTP_FROM")
    p.add_argument("--from", dest="from_addr", default="",
                   help="From (иначе SMTP_FROM или --email)")
    p.add_argument("--smtp-host", default="")
    p.add_argument("--smtp-port", default="")
    p.add_argument("--smtp-user", default="")
    p.add_argument("--smtp-password", default="")
    p.add_argument("--attach", default="", help="файл резюме, например CV.pdf")
    p.add_argument("--delay", type=float, default=8.0,
                   help="пауза между письмами, сек")
    p.add_argument("--limit", type=int, default=0, help="сколько писем за раз (0 = все)")
    p.add_argument("--include-guess", action="store_true",
                   help="также слать адреса с confidence=guess")
    p.add_argument("--sent-log", default="", help="куда писать уже отправленные")
    p.add_argument("--confirm", action="store_true",
                   help="без этого флага письма НЕ отправляются")
    p.add_argument("-o", "--out", default="")
    p.set_defaults(func=cmd_send)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)
