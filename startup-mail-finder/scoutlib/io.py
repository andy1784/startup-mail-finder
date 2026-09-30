from __future__ import annotations

import csv
import os
import sys
import urllib.parse


def write_csv(path, rows, fields=None):
    if not rows:
        print("[io] нет данных", file=sys.stderr)
        return
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fields = fields or list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})
    print(f"[io] {len(rows)} строк -> {path}", file=sys.stderr)


def read_csv(path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def canonical_domain(value: str) -> str:
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw.lstrip("/")
    host = urllib.parse.urlsplit(raw).netloc or raw
    return host.removeprefix("www.")


def merge_rows(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        if v in (None, ""):
            continue
        cur = out.get(k, "")
        if not cur:
            out[k] = v
            continue
        if k == "source" and v not in str(cur).split(","):
            out[k] = f"{cur},{v}"
        elif k == "founder" and v not in str(cur):
            out[k] = f"{cur}; {v}"
    return out


def merge_company_csvs(paths: list[str]) -> list[dict]:
    seen: dict[str, dict] = {}
    order: list[str] = []
    for path in paths:
        for row in read_csv(path):
            key = canonical_domain(row.get("domain") or row.get("website") or "")
            if not key:
                continue
            if key not in seen:
                seen[key] = dict(row)
                seen[key]["domain"] = key
                order.append(key)
            else:
                seen[key] = merge_rows(seen[key], row)
                seen[key]["domain"] = key
    return [seen[k] for k in order]
