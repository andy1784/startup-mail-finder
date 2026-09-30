from __future__ import annotations

import csv
import os
import re
import sys
import urllib.parse

from .io import write_csv

TEMPLATE_DE = """Subject: Bewerbung — {role} ({your_name})

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

TEMPLATE_EN = """Subject: Application — {role} ({your_name})

Hi {team},

I'm {your_name}, applying for the {role} role at {company}.

{why_us}

I've attached my resume. You can reach me at {your_email}.

Best,
{your_name}
{phone}

--
This message was written once, by hand, and is not an automated campaign.
If you'd rather not hear from me again, just say so and I won't follow up.
"""

TEMPLATES = {"en": TEMPLATE_EN, "de": TEMPLATE_DE}


def cmd_drafts(args):
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
                    "smtp": p.get("smtp", ""),
                    "website": p.get("website", ""), "domain": p.get("domain", "")}
                   for p in people]
    else:
        with open(args.input, newline="", encoding="utf-8") as f:
            rows_in = [r for r in csv.DictReader(f) if r.get("email")]

    lang = (getattr(args, "lang", None) or "en").lower()
    if lang not in TEMPLATES:
        sys.exit(f"[drafts] неизвестный --lang {lang}. Доступны: en, de")
    tpl = TEMPLATES[lang]
    if args.template:
        with open(args.template, encoding="utf-8") as fh:
            tpl = fh.read()

    outdir = args.outdir
    os.makedirs(outdir, exist_ok=True)
    rows = []
    for i, r in enumerate(rows_in, 1):
        name = (r.get("name") or "").strip() or r.get("domain", "")
        team = name.split()[0] if name else "Team"
        first = (r.get("person") or "").split()[0] if r.get("person") else ""
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
        parts = body.split("\n", 1)
        subj = parts[0]
        body = parts[1].lstrip("\n") if len(parts) > 1 else ""
        person = (r.get("person") or "").strip()
        suffix = f"_{person.split()[0]}" if person else ""
        stem = re.sub(r"[^\w.\-]+", "_", str(r.get("domain") or name))[:80] or "draft"
        fn = os.path.join(outdir, f"{i:03d}_{stem}{suffix}.txt")
        header = f"To: {r['email']}"
        notes = []
        if r.get("confidence") == "guess":
            notes.append("адрес построен по шаблону, не проверен")
        if r.get("smtp") == "catch-all":
            notes.append("MX catch-all: RCPT не доказывает существование ящика")
        if r.get("smtp") == "reject":
            notes.append("SMTP отклонил адрес")
        if notes:
            header += "   # ВНИМАНИЕ: " + "; ".join(notes)
        with open(fn, "w", encoding="utf-8") as fh:
            fh.write(f"{header}\n{subj}\n{body}")
        subj_text = subj.split(":", 1)[1].strip() if ":" in subj else subj.strip()
        rows.append({"to": r["email"], "person": r.get("person", ""),
                     "confidence": r.get("confidence", ""),
                     "smtp": r.get("smtp", ""),
                     "subject": subj_text, "draft": fn, "company": name,
                     "website": r.get("website", ""),
                     "mailto": "mailto:" + urllib.parse.quote(r["email"])
                               + "?subject=" + urllib.parse.quote(subj_text)})
    write_csv(args.out or os.path.join(outdir, "drafts_index.csv"), rows)
    warn = sum(1 for r in rows if r["confidence"] == "guess")
    print(f"[drafts] {len(rows)} черновиков ({lang}) -> {outdir}/", file=sys.stderr)
    if warn:
        print(f"[drafts] {warn} адресов построены по шаблону (confidence=guess). "
              f"Перед отправкой проверь их: неверный адрес хуже, чем общий info@.",
              file=sys.stderr)
    print("[drafts] Отправка — вручную, из своего почтового клиента: так ты "
          "видишь каждое письмо и не ловишь блок за спам.", file=sys.stderr)
