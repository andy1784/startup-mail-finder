#!/usr/bin/env python3
import os
import tempfile
import unittest
import urllib.parse
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from scoutlib.crawl import candidate_urls, harvest_emails_from_html, soup_of
from scoutlib.drafts import cmd_drafts
from scoutlib.emails import normalize_email, role_score, smtp_probe
from scoutlib.emails import clean_url
from scoutlib.http import get
from scoutlib.people import extract_people_from_ldjson, guess_emails, slug_name
from scoutlib.sources import overpass_query


class NormalizeEmailTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(normalize_email("Hello@AgeoSpatial.com"),
                         "hello@ageospatial.com")

    def test_mailto_query(self):
        self.assertEqual(
            normalize_email("mailto:jobs@startup.io?subject=Hi"),
            "jobs@startup.io")

    def test_html_entity(self):
        self.assertEqual(normalize_email("info&#64;startup.de"),
                         "info@startup.de")

    def test_placeholders_dropped(self):
        junk = [
            "you@email.com", "name@company.com", "jane@carrier.com",
            "jordan@acme.com", "name@yourcompany.com", "you@company.com",
            "noreply@x.com", "webmaster@x.com",
        ]
        for e in junk:
            self.assertIsNone(normalize_email(e), e)

    def test_deobfuscate(self):
        self.assertEqual(
            normalize_email("jobs (at) startup (dot) io"),
            "jobs@startup.io")


class UrlTests(unittest.TestCase):
    def test_clean_url_origin_only(self):
        self.assertEqual(clean_url("https://www.x.com/about?q=1"),
                         "https://www.x.com")

    def test_clean_url_adds_https(self):
        self.assertEqual(clean_url("example.io"), "https://example.io")

    def test_candidate_urls_dedup_and_same_host(self):
        html = """
        <a href="/contact">c</a>
        <a href="/contact/">c2</a>
        <a href="https://other.com/about">ext</a>
        <a href="https://www.x.com/team">team</a>
        """
        urls = candidate_urls("https://x.com", soup_of(html))
        paths = [urllib.parse.urlsplit(u).path.rstrip("/") for u in urls]
        self.assertEqual(len(paths), len(set(paths)))
        self.assertIn("/contact", paths)
        self.assertTrue(all("other.com" not in u for u in urls))


class HarvestTests(unittest.TestCase):
    def test_skips_placeholders_keeps_real(self):
        html = """
        <a href="mailto:hello@real.io">mail</a>
        you@email.com jane@carrier.com
        <script type="application/ld+json">
        {"@type":"Organization","email":"team@real.io","name":"Real"}
        </script>
        """
        found = harvest_emails_from_html(html)
        self.assertIn("hello@real.io", found)
        self.assertIn("team@real.io", found)
        self.assertNotIn("you@email.com", found)
        self.assertNotIn("jane@carrier.com", found)


class JsonLdTests(unittest.TestCase):
    def test_nested_graph(self):
        html = """
        <script type="application/ld+json">
        {"@graph":[{"@type":"Person","name":"Ada Lovelace",
                    "email":"ada@analytical.io"}]}
        </script>
        """
        soup = soup_of(html)
        people = extract_people_from_ldjson(soup)
        self.assertEqual(people, [("ada@analytical.io", "Ada Lovelace")])


class NameGuessTests(unittest.TestCase):
    def test_slug_umlaut_and_title(self):
        self.assertEqual(slug_name("Dr.-Ing. Johannes Gräbert"),
                         ("johannes", "graebert"))

    def test_guess_emails(self):
        g = guess_emails("Maaz Sheikh", "ageospatial.com")
        self.assertEqual(g[0], "maaz.sheikh@ageospatial.com")
        self.assertIn("maaz@ageospatial.com", g)


class RoleScoreTests(unittest.TestCase):
    def test_hr_local_part(self):
        self.assertEqual(role_score("hr@x.io"), 100)
        self.assertEqual(role_score("hello@x.io"), 60)
        self.assertEqual(role_score("support@x.io"), 60)
        self.assertEqual(role_score("founders@x.io"), 60)

    def test_this_is_not_hr(self):
        self.assertEqual(role_score("this@company.io"), 30)


class DraftsTests(unittest.TestCase):
    def _run(self, lang="en", template_body=None):
        with tempfile.TemporaryDirectory() as d:
            leads = os.path.join(d, "leads.csv")
            with open(leads, "w", encoding="utf-8") as f:
                f.write("email,name,domain\njobs@x.io,Acme,x.io\n")
            tpl = ""
            if template_body is not None:
                tpl = os.path.join(d, "tpl.txt")
                with open(tpl, "w", encoding="utf-8") as f:
                    f.write(template_body)
            outdir = os.path.join(d, "out")
            args = SimpleNamespace(
                people="", input=leads, template=tpl, outdir=outdir,
                out="", role="Engineer", why="I build data pipelines.",
                name="Ivan", email="ivan@x.io", phone="", lang=lang,
            )
            cmd_drafts(args)
            texts = []
            for fn in os.listdir(outdir):
                if fn.endswith(".txt"):
                    with open(os.path.join(outdir, fn), encoding="utf-8") as fh:
                        texts.append(fh.read())
            return texts

    def test_single_line_template_does_not_crash(self):
        texts = self._run(template_body="Subject: Hi {company}")
        self.assertTrue(texts)
        self.assertIn("Acme", texts[0])

    def test_english_template(self):
        texts = self._run(lang="en")
        self.assertIn("Application", texts[0])
        self.assertIn("Hi Team", texts[0])

    def test_german_template(self):
        texts = self._run(lang="de")
        self.assertIn("Bewerbung", texts[0])
        self.assertIn("Guten Tag", texts[0])


class PlacesPaginationTests(unittest.TestCase):
    def test_overpass_query_uses_name_filter(self):
        q = overpass_query("Berlin", ['"office"="company"'], 25, 10, "tech")
        self.assertIn('["name"~"tech"]', q)
        self.assertIn('["website"]', q)


class SmtpProbeTests(unittest.TestCase):
    def setUp(self):
        import scoutlib.emails as em
        em._SMTP_CACHE.clear()
        em._MX_CACHE.clear()

    def test_ok_not_catchall(self):
        smtp = MagicMock()
        smtp.rcpt.side_effect = [(250, b"ok"), (550, b"no")]
        smtp.__enter__.return_value = smtp
        smtp.__exit__.return_value = False
        with patch("scoutlib.emails.mx_hosts", return_value=["mx.x.io"]), \
             patch("smtplib.SMTP", return_value=smtp):
            self.assertEqual(smtp_probe("ada@x.io"), "ok")

    def test_catchall(self):
        smtp = MagicMock()
        smtp.rcpt.side_effect = [(250, b"ok"), (250, b"ok")]
        smtp.__enter__.return_value = smtp
        smtp.__exit__.return_value = False
        with patch("scoutlib.emails.mx_hosts", return_value=["mx.x.io"]), \
             patch("smtplib.SMTP", return_value=smtp):
            self.assertEqual(smtp_probe("ada@x.io"), "catch-all")

    def test_reject(self):
        smtp = MagicMock()
        smtp.rcpt.side_effect = [(550, b"no"), (550, b"no")]
        smtp.__enter__.return_value = smtp
        smtp.__exit__.return_value = False
        with patch("scoutlib.emails.mx_hosts", return_value=["mx.x.io"]), \
             patch("smtplib.SMTP", return_value=smtp):
            self.assertEqual(smtp_probe("ghost@x.io"), "reject")


class SslFallbackTests(unittest.TestCase):
    def test_logs_once_then_retries(self):
        import requests
        import scoutlib.http as httpmod
        httpmod._SSL_WARNED.clear()
        good = MagicMock()
        good.status_code = 200
        sess = MagicMock()
        sess.get.side_effect = [requests.exceptions.SSLError("bad cert"), good]
        with patch("scoutlib.http.session", return_value=sess):
            r = get("https://bad-cert.example/")
        self.assertIs(r, good)
        self.assertEqual(sess.get.call_count, 2)
        self.assertFalse(sess.get.call_args_list[0].kwargs.get("verify", True) is False)
        self.assertFalse(sess.get.call_args_list[1].kwargs.get("verify"))


class MergeResumeTests(unittest.TestCase):
    def test_merge_dedups_www_and_keeps_founder(self):
        from scoutlib.io import merge_company_csvs, write_csv
        with tempfile.TemporaryDirectory() as d:
            a = os.path.join(d, "a.csv")
            b = os.path.join(d, "b.csv")
            write_csv(a, [{"name": "Acme", "website": "https://www.acme.io",
                           "domain": "www.acme.io", "source": "yc"}])
            write_csv(b, [{"name": "Acme Inc", "website": "https://acme.io",
                           "domain": "acme.io", "founder": "Ada", "source": "wikidata"}])
            rows = merge_company_csvs([a, b])
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["domain"], "acme.io")
            self.assertEqual(rows[0]["founder"], "Ada")
            self.assertIn("yc", rows[0]["source"])
            self.assertIn("wikidata", rows[0]["source"])

    def test_emails_resume_skips_done(self):
        from scoutlib.crawl import cmd_emails
        with tempfile.TemporaryDirectory() as d:
            inp = os.path.join(d, "in.csv")
            out = os.path.join(d, "out.csv")
            with open(inp, "w", encoding="utf-8") as f:
                f.write("name,website,domain\nA,https://a.io,a.io\nB,https://b.io,b.io\n")
            with open(out, "w", encoding="utf-8") as f:
                f.write("name,website,domain,email\nA,https://a.io,a.io,hi@a.io\n")
            crawled = []

            def fake(company, *a, **k):
                crawled.append(company["name"])
                return dict(company, email="x@b.io", email_kind="role",
                            on_site="y", mx="ok", emails="x@b.io",
                            pages_crawled=1, js_rendered="", note="")

            args = SimpleNamespace(input=inp, out=out, workers=1,
                                   ignore_robots=False, verbose=False,
                                   js=False, resume=True)
            with patch("scoutlib.crawl.crawl_domain", side_effect=fake):
                cmd_emails(args)
            self.assertEqual(crawled, ["B"])
            from scoutlib.io import read_csv
            names = {r["name"] for r in read_csv(out)}
            self.assertEqual(names, {"A", "B"})


class SendTests(unittest.TestCase):
    def _index(self, d, to="jobs@x.io", subject="Hi", guess=""):
        draft = os.path.join(d, "001_x.io.txt")
        with open(draft, "w", encoding="utf-8") as f:
            f.write(f"To: {to}\nSubject: {subject}\n\nHello\n")
        idx = os.path.join(d, "drafts_index.csv")
        with open(idx, "w", encoding="utf-8") as f:
            f.write("to,subject,draft,confidence,smtp\n")
            f.write(f"{to},{subject},{draft},{guess},\n")
        return idx

    def test_dry_run_does_not_send(self):
        from scoutlib.send import cmd_send
        with tempfile.TemporaryDirectory() as d:
            idx = self._index(d)
            sent_log = os.path.join(d, "sent.log")
            args = SimpleNamespace(
                input=idx, outdir=d, name="Ivan", email="ivan@x.io",
                from_addr="ivan@x.io", smtp_host="smtp.example.com", smtp_port="587",
                smtp_user="", smtp_password="", attach="", delay=0, limit=0,
                include_guess=False, sent_log=sent_log, confirm=False, out="",
            )
            cmd_send(args)
            self.assertFalse(os.path.exists(sent_log))

    def test_skips_already_sent_and_guess(self):
        from scoutlib.send import cmd_send
        with tempfile.TemporaryDirectory() as d:
            idx = self._index(d, guess="guess")
            sent_log = os.path.join(d, "sent.log")
            with open(sent_log, "w", encoding="utf-8") as f:
                f.write("jobs@x.io\t2026-01-01\tHi\n")
            args = SimpleNamespace(
                input=idx, outdir=d, name="Ivan", email="ivan@x.io",
                from_addr="ivan@x.io", smtp_host="smtp.example.com", smtp_port="587",
                smtp_user="", smtp_password="", attach="", delay=0, limit=0,
                include_guess=False, sent_log=sent_log, confirm=False, out="",
            )
            cmd_send(args)

    def test_confirm_sends_via_smtp(self):
        from scoutlib.send import cmd_send
        with tempfile.TemporaryDirectory() as d:
            idx = self._index(d)
            sent_log = os.path.join(d, "sent.log")
            smtp = MagicMock()
            args = SimpleNamespace(
                input=idx, outdir=d, name="Ivan", email="ivan@x.io",
                from_addr="ivan@x.io", smtp_host="smtp.example.com", smtp_port="587",
                smtp_user="ivan@x.io", smtp_password="pw", attach="", delay=0,
                limit=0, include_guess=False, sent_log=sent_log, confirm=True, out="",
            )
            with patch("scoutlib.send._open_smtp", return_value=smtp):
                cmd_send(args)
            smtp.send_message.assert_called_once()
            with open(sent_log, encoding="utf-8") as f:
                self.assertIn("jobs@x.io", f.read())


if __name__ == "__main__":
    unittest.main()
