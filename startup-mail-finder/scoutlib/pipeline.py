from __future__ import annotations

import os
import sys
from argparse import Namespace

from .crawl import cmd_emails
from .drafts import cmd_drafts
from .io import merge_company_csvs, write_csv
from .people import cmd_people
from .sources import cmd_osm, cmd_wikidata, cmd_yc


def cmd_merge(args):
    paths = list(getattr(args, "merge_files", None) or [])
    paths.extend(args.inputs or [])
    if not paths:
        sys.exit("[merge] укажи файлы: scout.py merge -i a.csv b.csv -o companies.csv")
    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        sys.exit("[merge] нет файлов: " + ", ".join(missing))
    rows = merge_company_csvs(paths)
    write_csv(args.out, rows)
    print(f"[merge] {len(rows)} уникальных доменов из {len(paths)} файлов -> {args.out}",
          file=sys.stderr)


def cmd_run(args):
    os.makedirs(args.workdir, exist_ok=True)
    companies = os.path.join(args.workdir, "companies.csv")
    leads = os.path.join(args.workdir, "leads.csv")
    people = os.path.join(args.workdir, "people.csv")
    outreach = os.path.join(args.workdir, "outreach")
    parts = []

    sources = [s.strip() for s in (args.sources or "yc").split(",") if s.strip()]
    for src in sources:
        out = os.path.join(args.workdir, f"companies_{src}.csv")
        if src == "yc":
            cmd_yc(Namespace(limit=args.limit, max_pages=args.max_pages,
                             batch=args.batch, token=args.token, out=out))
        elif src == "osm":
            if not args.city:
                sys.exit("[run] для osm нужен --city")
            cmd_osm(Namespace(city=args.city, limit=args.limit, filter="",
                              only_startups=True, name_filter="", tag="",
                              include="", timeout=45, out=out))
        elif src == "wikidata":
            cmd_wikidata(Namespace(limit=args.limit, lang="en",
                                   countries=args.countries, tech_only=True,
                                   out=out))
        else:
            sys.exit(f"[run] неизвестный источник '{src}'. Доступны: yc,osm,wikidata")
        parts.append(out)

    cmd_merge(Namespace(merge_files=parts, inputs=[], out=companies))
    cmd_emails(Namespace(input=companies, out=leads, workers=args.workers,
                         ignore_robots=False, js=args.js, verbose=False,
                         resume=True))
    cmd_people(Namespace(input=companies, out=people, workers=max(1, args.workers - 1),
                         max_per_company=3, ignore_robots=False,
                         verify_smtp=args.verify_smtp, resume=True))
    if args.name and args.email and args.role:
        cmd_drafts(Namespace(input=leads, people=people if args.draft_people else "",
                             name=args.name, email=args.email, role=args.role,
                             why=args.why, phone=args.phone, lang=args.lang,
                             template="", outdir=outreach, out=""))
    print(f"[run] готово: {companies}, {leads}, {people}", file=sys.stderr)
