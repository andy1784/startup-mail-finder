from __future__ import annotations

import re

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0 Safari/537.36 +contact-via-site"
)
UA_API = "startup-mail-scout/1.0 (OSM Overpass client; python-requests)"
TIMEOUT = 9
MAX_PAGES_PER_SITE = 6
MAX_QUEUE = 12

CONTACT_PATHS = [
    "/contact", "/contact/", "/kontakt", "/about", "/about-us", "/company",
    "/team", "/careers", "/jobs", "/impressum", "/about/contact", "/hello",
]
CONTACT_HINTS = ("contact", "kontakt", "about", "career", "job", "team",
                 "company", "impressum", "press", "join")

ROLE_PRIORITY = [
    (re.compile(r"^(hr|jobs|recruit\w*|talent|people|hiring|careers?|bewerbung|"
                r"jobsuche|personal|recruitment|join)@", re.I), 100),
    (re.compile(r"(?:^|[._+\-])(hr|recruit|talent|hiring)(?:[._+\-]|@)", re.I), 100),
    (re.compile(r"^(hello|hallo|hi|info|contact|kontakt|office|team|mail|mailbox|"
                r"enquiries|inquiries|sekretariat|welcome|willkommen|sales|"
                r"press|pr|partners|hello-world|support|founders?)@", re.I), 60),
]
BAD = re.compile(
    r"^(no-?reply|noreply|donotreply|mailer-daemon|postmaster|abuse|"
    r"webmaster|root|@|\.)|example\.(com|org|net)|sentry|wixpress|"
    r"@2x|sentry\.io|godaddy|domain\.com", re.I)
PLACEHOLDER_EMAIL = re.compile(
    r"^(?:you|your|name|user|username|email|test|foo|bar|jane\.?doe|"
    r"john\.?doe|n/?a)@|"
    r"@(?:example|test|domain|company|yourcompany|email|acme|carrier|"
    r"restaurant|placeholder|sample|foo|bar|localhost|yourdomain|"
    r"domainname)\.",
    re.I)
EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,24}")
FILE_SUFFIX_RE = re.compile(
    r"\.(png|jpe?g|gif|svg|webp|css|js|ico|woff2?|ttf|eot|pdf|zip|mp4)$", re.I)
BAD_ROLEBOX = re.compile(r"^(datenschutz|privacy|impressum|legal|abuse|dpo|"
                         r"gdpr|schadensmeldung|presse|redaktion|newsroom|"
                         r"postmaster|webmaster|hostmaster)@", re.I)
DEOBF = [
    (re.compile(r"\s*[\(\[\{]\s*(?:at|@)\s*[\)\]\}]\s*", re.I), "@"),
    (re.compile(r"\s*(?:\[dot\]|\(dot\)|\{dot\}|\sdot\s)\s*", re.I), "."),
    (re.compile(r"\s+(?:at|@)\s+", re.I), "@"),
    (re.compile(r"^mailto:", re.I), ""),
]
FREEMAIL = ("gmail.", "googlemail.", "outlook.", "hotmail.", "yahoo.", "icloud.",
            "hey.com", "proton.me", "protonmail.", "fastmail.", "aol.", "me.com")
CHROME_CANDIDATES = ["/usr/bin/chromium", "/usr/bin/chromium-browser",
                     "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"]
PEOPLE_PAGES = ["/impressum", "/impressum/", "/about", "/about-us", "/team",
                "/company", "/contact", "/kontakt", "/about/company",
                "/en/about", "/de/impressum", "/en/impressum"]
UMLAUTS = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "Ae", "Ö": "Oe",
           "Ü": "Ue"}
TITLE_RE = re.compile(r"^(Dr|Prof|Ing|Dipl|PhD|Phd|MSc|MBA|BSc|MA)\b", re.I)
