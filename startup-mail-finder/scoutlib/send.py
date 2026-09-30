from __future__ import annotations

import csv
import os
import smtplib
import ssl
import sys
import time
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from .io import write_csv


def _smtp_settings(args):
    host = args.smtp_host or os.environ.get("SMTP_HOST", "")
    port = int(args.smtp_port or os.environ.get("SMTP_PORT", "587"))
    user = args.smtp_user or os.environ.get("SMTP_USER", "")
    password = args.smtp_password or os.environ.get("SMTP_PASSWORD", "")
    from_addr = args.from_addr or os.environ.get("SMTP_FROM", "") or args.email
    return host, port, user, password, from_addr


def _load_sent(path: str) -> set[str]:
    if not path or not os.path.exists(path):
        return set()
    out = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            addr = line.strip().split("\t", 1)[0].strip().lower()
            if addr and not addr.startswith("#"):
                out.add(addr)
    return out


def _parse_draft(path: str) -> tuple[str, str, str]:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    lines = text.splitlines()
    to = ""
    subject = ""
    body_start = 0
    for i, line in enumerate(lines):
        if line.lower().startswith("to:"):
            to = line.split(":", 1)[1].split("#", 1)[0].strip()
            body_start = i + 1
        elif line.lower().startswith("subject:"):
            subject = line.split(":", 1)[1].strip()
            body_start = i + 1
            break
    body = "\n".join(lines[body_start:]).lstrip("\n")
    return to, subject, body


def _build_message(from_addr, from_name, to, subject, body, attach) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, from_addr)) if from_name else from_addr
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid()
    msg.set_content(body)
    if attach:
        with open(attach, "rb") as f:
            data = f.read()
        name = os.path.basename(attach)
        if name.lower().endswith(".pdf"):
            msg.add_attachment(data, maintype="application", subtype="pdf", filename=name)
        else:
            msg.add_attachment(data, maintype="application", subtype="octet-stream", filename=name)
    return msg


def _open_smtp(host, port, user, password):
    context = ssl.create_default_context()
    if port == 465:
        smtp = smtplib.SMTP_SSL(host, port, timeout=30, context=context)
    else:
        smtp = smtplib.SMTP(host, port, timeout=30)
        smtp.ehlo()
        smtp.starttls(context=context)
        smtp.ehlo()
    if user:
        smtp.login(user, password)
    return smtp


def cmd_send(args):
    index = args.input or os.path.join(args.outdir, "drafts_index.csv")
    if not os.path.exists(index):
        sys.exit(f"[send] нет индекса черновиков: {index}")
    with open(index, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        sys.exit("[send] индекс пуст")

    host, port, user, password, from_addr = _smtp_settings(args)
    if not host:
        sys.exit("[send] задай SMTP_HOST или --smtp-host (например smtp.gmail.com)")
    if not from_addr:
        sys.exit("[send] задай --from или --email / SMTP_FROM")
    if args.confirm and not password and user:
        sys.exit("[send] задай SMTP_PASSWORD или --smtp-password")

    sent_path = args.sent_log or os.path.join(args.outdir, "sent.log")
    already = _load_sent(sent_path)
    skipped_guess = 0
    queue = []
    for r in rows:
        to = (r.get("to") or "").strip()
        draft = r.get("draft") or ""
        if not to or not draft or not os.path.exists(draft):
            continue
        if to.lower() in already:
            continue
        if r.get("confidence") == "guess" and not args.include_guess:
            skipped_guess += 1
            continue
        if r.get("smtp") == "reject":
            continue
        queue.append(r)

    limit = args.limit if args.limit else len(queue)
    queue = queue[:limit]
    mode = "SEND" if args.confirm else "DRY-RUN"
    print(f"[send] {mode}: {len(queue)} писем, from={from_addr}, smtp={host}:{port}",
          file=sys.stderr)
    if skipped_guess:
        print(f"[send] пропуск {skipped_guess} guess-адресов (нужен --include-guess)",
              file=sys.stderr)
    if already:
        print(f"[send] уже в {sent_path}: {len(already)}", file=sys.stderr)
    if not queue:
        print("[send] нечего отправлять", file=sys.stderr)
        return
    if not args.confirm:
        for r in queue:
            print(f"  would send -> {r['to']}  {r.get('subject', '')}", file=sys.stderr)
        print("[send] ничего не отправлено. Перечитай черновики, затем добавь --confirm",
              file=sys.stderr)
        return

    log_rows = []
    smtp = None
    try:
        smtp = _open_smtp(host, port, user, password)
        os.makedirs(os.path.dirname(sent_path) or ".", exist_ok=True)
        with open(sent_path, "a", encoding="utf-8") as log:
            for i, r in enumerate(queue, 1):
                to, subject, body = _parse_draft(r["draft"])
                if not to:
                    to = r["to"]
                if not subject:
                    subject = r.get("subject", "")
                msg = _build_message(from_addr, args.name, to, subject, body, args.attach)
                smtp.send_message(msg)
                log.write(f"{to.lower()}\t{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\t{subject}\n")
                log.flush()
                log_rows.append({"to": to, "subject": subject, "status": "sent",
                                 "draft": r.get("draft", "")})
                print(f"  sent {i}/{len(queue)} -> {to}", file=sys.stderr)
                if i < len(queue):
                    time.sleep(max(0.0, args.delay))
    except smtplib.SMTPException as e:
        sys.exit(f"[send] SMTP: {e}")
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except Exception:
                pass
    if log_rows:
        write_csv(args.out or os.path.join(args.outdir, "sent.csv"), log_rows)
    print(f"[send] отправлено {len(log_rows)} -> {sent_path}", file=sys.stderr)
