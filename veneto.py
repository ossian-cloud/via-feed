"""Fetch the Regione del Veneto environmental procedures open for public observations. The region keeps one
hand-edited page per year of filing ("Progetti presentati nel 2026"), one block per project with the proponent, a
link to the documents, the observation deadline written in words, and the status. Two requests (this year and last
year: a project filed in 2025 can have a consultation open now), 10 s apart as its robots.txt asks.

Usage: python3 veneto.py
Writes data/veneto.json (dict id -> record). Refuses to run while data/BLOCKED-VENETO exists.
"""
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

from fetch import UA, scrub, KEEP_CLOSED_DAYS
from sigle import SIGLA_PROVINCE, SIGLA_REGION

PAGE = "https://www.regione.veneto.it/web/vas-via-vinca-nuvv/progetti-{year}"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "veneto.json")
BLOCKED = os.path.join(HERE, "data", "BLOCKED-VENETO")
AUTHORITY = "Regione del Veneto"
PAUSE = 10  # robots.txt Crawl-delay
MONTHS = "gennaio febbraio marzo aprile maggio giugno luglio agosto settembre ottobre novembre dicembre".split()
TYPES = [("PAUR", "PAUR (VIA regionale, art. 27-bis)"), ("SCREENING", "Verifica di Assoggettabilità a VIA"),
         ("SCOPING", "Definizione dei contenuti dello studio di impatto ambientale (scoping)"),
         ("VALUTAZIONE PRELIMINARE", "Valutazione preliminare (art. 6, c. 9)")]


def to_text(h):
    """Page text with each link kept as [[url]] before its text; one line per block element."""
    h = re.sub(r"(?s)<(script|style).*?</\1>", "", h)
    h = re.sub(r'<a\s[^>]*href="([^"]+)"[^>]*>', lambda m: " [[" + html.unescape(m[1]) + "]] ", h)
    t = html.unescape(re.sub(r"<[^>]+>", "\n", h)).replace("\xa0", " ")
    return re.sub(r"\n\s*\n+", "\n", re.sub(r"[ \t]+", " ", t))


def deadline(s):
    """The latest date after any 'SCADENZA' label: reopened terms add 'NUOVA DATA SCADENZA OSSERVAZIONI ...: <date>'."""
    ds = [f"{m[3]}-{MONTHS.index(m[2].lower()) + 1:02d}-{int(m[1]):02d}" for m in
          re.finditer(r"SCADENZA(?:[^\n]*\n)?\s*(\d{1,2})°?\s+(" + "|".join(MONTHS) + r")\s+(\d{4})", s, re.I)]
    return max(ds) if ds else None


# 'Comuni di localizzazione:', 'Comune interessato:', and the page's typos ('localizzaizione', 'Comuneidi')
LABEL = r"[Cc][Oo][Mm][Uu][Nn][EeIi]\s*(?:di|i)?\s*(?:[Ll]ocal\w*|[Ii]nteressat\w*)\s*:?"


def places(title):
    """'Comuni di localizzazione: Melara (RO) e Ostiglia (MN) - Comuni interessati: Adria (RO)' ->
    (comuni, province codes). A province code applies to the names before it."""
    comuni, codes = [], []
    for seg in re.split(LABEL, title)[1:]:
        pending = []
        for tok in re.split(r"(\(\s*[A-Z]{2}\s*\))", seg):
            code = re.fullmatch(r"\(\s*([A-Z]{2})\s*\)", tok)
            if code:
                comuni += pending
                codes.append(code[1])
                pending = []
            else:
                pending += [x.strip(" .;-") for x in re.split(r",|;|\s+e\s+|\s+-\s+", tok) if x.strip(" .;-")]
        comuni += pending
    accent = {"a": "à", "e": "è", "i": "ì", "o": "ò", "u": "ù"}
    comuni = [re.sub(r"([aeiou])'$", lambda m: accent[m[1]], c) for c in comuni]  # Rosa' -> Rosà
    return list(dict.fromkeys(comuni)), list(dict.fromkeys(c for c in codes if c in SIGLA_PROVINCE))


def parse(h, page_url):
    t = to_text(h)
    parts = re.split(r"(?:^|\n|\s)(\d+(?:/\d\d)?)\.\s*Proponente:\s*", t)
    out = []
    for i in range(1, len(parts), 2):
        num, body, before = parts[i], parts[i + 1], parts[i - 1]
        kinds = [(before.upper().rfind(k), lab) for k, lab in TYPES]
        kind = max(kinds)[1] if max(kinds)[0] >= 0 else None
        proponente = body.split("\n", 1)[0].strip()
        link = re.search(r"\[\[(https?://[^\]]+)\]\]\s*(.*?)\s*DATA SCADENZA", body, re.S)
        title = re.sub(r"\s+", " ", link[2]).strip() if link else None
        before_info = re.split(r"Per informazioni", body)[0] if "Per informazioni" in body else ""
        lines = [re.sub(r"\[\[[^\]]*\]\]", "", l).strip() for l in before_info.split("\n")]
        status = next((l for l in reversed(lines) if l), "")
        if "SCADENZA" in status.upper() or re.fullmatch(r"\d{1,2}°?\s+\w+\s+\d{4}", status):
            status = ""
        out.append({"num": num, "kind": kind, "proponente": proponente, "url": link[1] if link else page_url,
                    "title": title, "termine": deadline(body),
                    "stato": status if status and not status.startswith("Per informazioni") else None})
    return out


def record(x, year, old, today, now):
    comuni, codes = places(x["title"] or "")
    rid = f"veneto-{year}-{x['num'].split('/')[0]}"
    rec = {"id": rid, "authority": AUTHORITY, "codice": f"{x['num']}" if "/" in x["num"] else f"{x['num']}/{str(year)[2:]}",
           "url": x["url"], "procedure_type": x["kind"] or "Procedura di VIA regionale", "tipologia": None,
           "title": re.split(r"[.;]?\s+-?\s*" + LABEL, x["title"])[0].strip(" .;-"),
           "proponente": x["proponente"] or None, "regioni": sorted({SIGLA_REGION[c] for c in codes}) or ["Veneto"],
           "province": [SIGLA_PROVINCE[c] for c in codes], "comuni": comuni, "avvio": None,
           "termine_osservazioni": x["termine"], "termini": [x["termine"]], "stato": x["stato"],
           "first_seen": old.get("first_seen", today), "last_seen": today, "closed_at": None,
           "updated_at": now if old.get("termine_osservazioni") != x["termine"] else old.get("updated_at", now)}
    return scrub(rec)


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "it"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def run():
    if os.path.exists(BLOCKED):
        sys.exit(f"data/BLOCKED-VENETO exists, not running: {open(BLOCKED).read().strip()}")
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = now[:10]
    year = int(today[:4])
    procs = json.load(open(DATA, encoding="utf-8")) if os.path.exists(DATA) else {}
    seen, nrows = {}, 0
    for y in (year, year - 1):
        url = PAGE.format(year=y)
        try:
            rows = parse(get(url), url)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                open(BLOCKED, "w").write(f"{now} HTTP {e.code} on {url}\n")
            sys.exit(f"veneto: HTTP {e.code} on {url}")
        if len(rows) < 10 and (y < year or today[5:] > "03-01"):
            sys.exit(f"veneto: parsed only {len(rows)} blocks on {url}: layout changed? nothing written")
        nrows += len(rows)
        for x in rows:
            if x["termine"] and x["termine"] >= today and x["title"]:
                rec = record(x, y, procs.get(f"veneto-{y}-{x['num'].split('/')[0]}", {}), today, now)
                seen[rec["id"]] = rec
        time.sleep(PAUSE)
    for pid, r in procs.items():
        if pid not in seen and not r.get("closed_at"):
            r["closed_at"] = today
    procs.update(seen)
    limit = (dt.date.fromisoformat(today) - dt.timedelta(days=KEEP_CLOSED_DAYS)).isoformat()
    procs = {k: v for k, v in procs.items() if not v.get("closed_at") or v["closed_at"] >= limit}
    os.makedirs(os.path.dirname(DATA), exist_ok=True)
    with open(DATA + ".tmp", "w", encoding="utf-8") as f:
        json.dump(procs, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(DATA + ".tmp", DATA)
    print(f"veneto: {nrows} blocks, {len(seen)} open, stored {len(procs)}", file=sys.stderr)


if __name__ == "__main__":
    run()
