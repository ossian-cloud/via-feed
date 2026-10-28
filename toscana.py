"""Fetch the Regione Toscana environmental-assessment procedures open for public observations from the Region's GeA
"avvisi pubblici" (Star-Info) list: https://servizi.patti.regione.toscana.it/star-info/ente/GEA?dominio=GEA&destinazione=avvisiPubblici
The page itself reads one JSON list, POST https://servizi.toscana.it/pub-api/suap/services/avvisi/eventiPubblici/{page}/{size}
(size 100, ~3 pages for ~210 notices since 2025-03, concluded ones included). robots.txt of servizi.toscana.it (checked by hand
2026-10-28) disallows only /cgi-bin/; the pages are on servizi.patti.regione.toscana.it, whose robots.txt answers 302 to a
404 page. Requests per run: one per page of 100 (MAX_REQUESTS at most), PAUSE seconds apart, no detail calls.

Deadline: the field "dataFinePubblicazione", shown by the page as "Data Fine Pubblicazione" (end of the publication of the
notice, which is the period for observations: 30 days in nearly every notice). Both dates are local midnight stored as UTC
("2026-11-20T23:00:00Z" = 21 Nov 2026 in Rome), so the date is read in the Europe/Rome zone. No other date is used as a
deadline. The notices' "stato" text is NOT used: it is stale on some notices (still "Consultazione in corso" after the end).

Rules:
- Included: notices whose end date is today or later, of the types that are VIA procedures (see TYPES), and whose
  publication window is at most MAX_WINDOW days (a notice with a longer window is a different kind of notice, e.g. a 3-year
  extension of terms).
- Excluded types: "Proroga dei termini" and "Verifica di ottemperanza" (no observations are collected), and anything unknown.
- National procedures are excluded if one ever appears (type or text naming the Ministry / "competenza statale"); this list
  is the Region's own, so none were seen on 2026-10-28.
- Title: the notice's short description with the id prefix, the law citations and the proponent clause removed, plus the
  comuni when the text does not name them. Link: the public detail page /star-info/avvisiPubblici/<id>?dominio=GEA.
- Comuni: the "comune" field is upper case and either comma-separated or not (two comuni run together); it is matched
  against ISTAT's list of comuni (../bandi/ref/comuni.csv) to give names, provinces and regions (some Toscana notices
  cover comuni of Lazio or Umbria). A name that does not match is kept as written, without province.
- Proponent: scrubbed with fetch.scrub/personlike; a single glued word of 13+ letters (e.g. a name run together)
  is dropped too. The notices' contact persons are never read.

Usage: python3 toscana.py
Writes data/toscana.json (dict id -> record). Refuses to run while data/BLOCKED-TOSCANA exists.
"""
import csv
import datetime as dt
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

from fetch import UA, scrub, personlike, KEEP_CLOSED_DAYS
from sigle import SIGLA_PROVINCE

API = "https://servizi.toscana.it/pub-api/suap/services/avvisi/eventiPubblici/{page}/{size}?dominio=GEA&codiceTerritorio=GEA"
DETAIL = "https://servizi.patti.regione.toscana.it/star-info/avvisiPubblici/{id}?dominio=GEA"
LIST_PAGE = "https://servizi.patti.regione.toscana.it/star-info/ente/GEA?dominio=GEA&destinazione=avvisiPubblici"
BODY = json.dumps({"sortField": "stato", "order": "desc", "sezione": "GEA"}).encode()
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "toscana.json")
BLOCKED = os.path.join(HERE, "data", "BLOCKED-TOSCANA")
COMUNI_CSV = os.path.join(HERE, "..", "bandi", "ref", "comuni.csv")
AUTHORITY = "Regione Toscana"
ROME = ZoneInfo("Europe/Rome")
SIZE = 100
PAUSE = 10
MAX_REQUESTS = 5
MAX_WINDOW = 100
TYPES = {"verifica di assoggettabilità a via": "Verifica di Assoggettabilità a VIA",
         "verifica di assoggettabilità (d.lgs. 152/2006 art. 19, l.r. 10/2010 art. 48)": "Verifica di Assoggettabilità a VIA",
         "procedimento autorizzatorio unico regionale (p.a.u.r.)": "PAUR (VIA regionale, art. 27-bis)",
         "valutazione di impatto ambientale": "Procedura di VIA regionale",
         "valutazione preliminare": "Valutazione preliminare (art. 6, c. 9)",
         "modifica di prescrizioni": "Modifica di prescrizioni di un provvedimento di VIA"}
NATIONAL = re.compile(r"competenza (?:statale|nazionale)|\bmase\b|\bmite\b|ministero dell['’]ambiente", re.I)


def norm(s):
    s = unicodedata.normalize("NFKD", (s or "").replace("ß", "ss")).encode("ascii", "ignore").decode()
    return " ".join(s.upper().replace("'", " ").replace("-", " ").split())


def load_comuni():
    """normalized name -> list of (ISTAT name, province sigla, region), Toscana first."""
    out = {}
    with open(COMUNI_CSV, encoding="latin-1", newline="") as f:
        rows = csv.reader(f, delimiter=";")
        next(rows)
        for r in rows:
            if len(r) > 14 and r[14].strip() in SIGLA_PROVINCE:
                region = r[10].strip().split("/")[0]
                out.setdefault(norm(r[6]), []).append((r[6].strip(), r[14].strip(), region))
    for v in out.values():
        v.sort(key=lambda t: t[2] != "Toscana")
    return out


def split_comuni(s, comuni):
    """'CASTELFIORENTINO, GAMBASSI TERME' or 'LATERINA PERGINE VALDARNO' -> [(ISTAT name | written name, sigla | None, region | None)]."""
    out = []
    for part in re.split(r"\s*[,;/]\s*|\s+-\s+", s or ""):
        words = norm(part).split()
        while words:
            for n in range(len(words), 0, -1):  # longest known comune first
                hit = comuni.get(" ".join(words[:n]))
                if hit:
                    out.append(hit[0])
                    words = words[n:]
                    break
            else:  # nothing matches: keep the rest as one written name
                out.append((" ".join(w[:1] + w[1:].lower() for w in words), None, None))
                words = []
    return list(dict.fromkeys(out))


LAW = re.compile(r"^\W*(?:art\.?\s*\d+\s+del\s+)?d\.?\s?lgs\.?\s*152/2006(?:,?\s*(?:ed?\s+)?art\.?\s*\d+(?:\s+della)?)?"
                 r"[\s,;ed]*(?:art\.?\s*\d+\s+della\s+)?l\.?\s?r\.?\s*10/2010(?:,?\s*art\.?\s*\d+)?\s*[.;]\s*", re.I)
LEAD = re.compile(r"^(?:paur ex .{0,100}?relativamente al progetto\s*|(?:reiterazione del )?(?:procedimento di )?verifica di assoggettabilit\S*\s*(?:a via\s*|regionale\s*)?"
                  r"(?:di competenza regionale\s*)?,?\s*(?:relativ\w+ (?:al|alla|all')\s*|per (?:il|la)\s*|riguardante (?:il|la)\s*)?(?:progetto\s*(?:di|degli|del|della|dell')?\s*)?)", re.I)
PROPONENT = re.compile(r"[;,.\s-]*\(?\bpropon\w*\s*:.*$", re.I | re.S)
PERSON_REF = re.compile(r"\b(?i:sig\.?(?:ra|\.ra)?|dott\.?(?:ssa)?|ing\.?|arch\.?|geom\.?|ditta)\s+(?:[A-ZÀ-Ý][\wÀ-ÿ']+)(?:\s+(?:[A-ZÀ-Ý][\wÀ-ÿ']+)){0,2}")


def sentence(s, dropped=None):
    """dropped: the raw proponent name when it was dropped as a person: its name is replaced by "la ditta"."""
    s = re.sub(r"\s+", " ", s.replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')).strip()
    s = PROPONENT.sub("", s)
    if dropped and len(dropped.strip()) > 3:
        s = re.sub(r"(?<![\wÀ-ÿ])" + r"\s+".join(re.escape(w) for w in dropped.split()) + r"(?![\wÀ-ÿ])", "la ditta", s, flags=re.I)
    s = PERSON_REF.sub("la ditta", s)
    s = re.sub(r"\b(del|dal|al)\s+la ditta", lambda m: {"del": "della", "dal": "dalla", "al": "alla"}[m[1].lower()] + " ditta", s)
    s = re.sub(r"\s*\[\s*(?:ID|CUP)[^\]]*\]", "", s, flags=re.I)  # "[ID 2744 - GEA 26971]", "[CUP: ...]"
    s = s.strip(" -")
    s = re.sub(r"^art\.?\s*\d+\s+l\.?\s?r\.?\s*10/2010\s*[.;]\s*", "", s, flags=re.I)
    s = LAW.sub("", s, count=1)
    s = LEAD.sub("", s, count=1).strip(" ,;.-")
    s = re.sub(r"[,\s]*(?:situato |ubicato )?nel(?:l[ei'])? ?Comun[ei](?:/i)? di [A-Z' ,]+$", "", s)  # the comuni are added back by title_of
    s = re.sub(r",\s*\(", " (", s)
    s = re.sub(r"^[Ll]e (?=modifiche)", "", s)
    if s.startswith('"'):  # a title that is all one quotation: drop that pair of quotes
        s = s[1:].replace('"', "", 1)
    if s.count('"') % 2:
        s = s.rstrip('"') if s.endswith('"') else s.replace('"', "", 1)
    letters = [c for c in s if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.6:  # the source sometimes writes all in capitals
        s = s.lower()
        s = re.sub(r"\b(paur|via|vas|vinca|aia|srt|sr|ss|sp|ade|bess)\b", lambda m: m[1].upper(), s)
    return (s[:1].upper() + s[1:]).rstrip(" ,;.")


def title_of(desc, comuni, dropped=None):
    t = sentence(desc, dropped)
    if len(t) > 240:
        t = t[:240].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"
    names = [c[0] for c in comuni]
    if names and not any(norm(n) in norm(t) for n in names):
        shown = names[:3] if len(names) > 3 else names
        t += " - " + ", ".join(shown) + (f" e altri {len(names) - 3} comuni" if len(names) > 3 else "")
    return t


def local_date(ts):
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ROME).date()


def record(e, c, kind, old, today, now, comuni):
    start, end = local_date(e["dataInizioPubblicazione"]), local_date(e["dataFinePubblicazione"])
    cs = split_comuni(c.get("comune"), comuni)
    rec_desc = e.get("descrizione") or c.get("opera") or ""
    siglas = list(dict.fromkeys(s for _, s, _ in cs if s))
    regions = sorted({r for _, _, r in cs if r} | {"Toscana"})
    prop = (c.get("proponente") or "").strip()
    dropped = None
    if personlike(prop) or (re.fullmatch(r"[^\W\d_]{13,}", prop)):
        dropped, prop = prop, ""
    rec = {"id": f"toscana-{e['id']}", "authority": AUTHORITY, "codice": f"GeA {e['id']}", "url": DETAIL.format(id=e["id"]),
           "procedure_type": TYPES[kind], "tipologia": None,
           "title": title_of(rec_desc, cs, dropped), "proponente": re.sub(r"\s+", " ", prop) or None,
           "regioni": regions, "province": [SIGLA_PROVINCE[s] for s in siglas], "comuni": [n for n, _, _ in cs],
           "avvio": start.isoformat(), "termine_osservazioni": end.isoformat(), "termini": [end.isoformat()], "stato": None,
           "first_seen": old.get("first_seen", today), "last_seen": today, "closed_at": None,
           "updated_at": now if old.get("termine_osservazioni") != end.isoformat() else old.get("updated_at", now)}
    return scrub(rec)


def post(page):
    req = urllib.request.Request(API.format(page=page, size=SIZE), data=BODY, method="POST",
                                 headers={"User-Agent": UA, "Accept-Language": "it", "Content-Type": "application/json",
                                          "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def run():
    if os.path.exists(BLOCKED):
        sys.exit(f"data/BLOCKED-TOSCANA exists, not running: {open(BLOCKED).read().strip()}")
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = dt.datetime.now(ROME).date().isoformat()
    procs = json.load(open(DATA, encoding="utf-8")) if os.path.exists(DATA) else {}
    items, total = {}, None
    for page in range(MAX_REQUESTS):
        try:
            d = post(page)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                open(BLOCKED, "w").write(f"{now} HTTP {e.code} on {API.format(page=page, size=SIZE)}\n")
            sys.exit(f"toscana: HTTP {e.code} on page {page}")
        total = d.get("totalElements")
        got = d.get("elements") or []
        for e in got:
            items[e["id"]] = e
        if not got or (total is not None and (page + 1) * SIZE >= total):
            break
        time.sleep(PAUSE)
    if not isinstance(total, int) or len(items) != total or total < 50:  # 206 on 2026-10-28; paging over an unstable sort loses items
        sys.exit(f"toscana: got {len(items)} distinct notices, the list says {total}: nothing written")
    comuni = load_comuni()
    seen, national, skipped, long_window, bad_date, bad_content = {}, 0, 0, 0, 0, 0
    for e in items.values():
        try:
            end, start = local_date(e["dataFinePubblicazione"]), local_date(e["dataInizioPubblicazione"])
        except (KeyError, TypeError, ValueError, AttributeError):
            bad_date += 1  # unknown end date: might be open, so it is never read as "closed"
            continue
        if end.isoformat() < today:
            continue
        kind = (e.get("procedimento") or "").strip().lower()
        if kind not in TYPES:
            kind = kind.rstrip(") ")  # "Modifica di prescrizioni)" on one notice
        if kind not in TYPES:
            skipped += 1
            continue
        if (end - start).days > MAX_WINDOW:
            long_window += 1
            continue
        try:
            c = json.loads(e["contenuto"], strict=False)
        except (TypeError, ValueError):
            bad_content += 1
            print(f"toscana: unparsable contenuto on notice {e['id']} (open until {end})", file=sys.stderr)
            continue
        if NATIONAL.search(kind):
            national += 1
            continue
        rec = record(e, c, kind, procs.get(f"toscana-{e['id']}", {}), today, now, comuni)
        seen[rec["id"]] = rec
    if bad_date or bad_content:
        sys.exit(f"toscana: {bad_date} notices without a readable date and {bad_content} open ones with unreadable content "
                 "(format changed?): nothing written")
    stored_open = {k for k, v in procs.items() if not v.get("closed_at")}
    if not seen and stored_open:
        sys.exit("toscana: nothing parsed but open records are stored: nothing written")
    still_open = {k for k in stored_open if (procs[k].get("termine_osservazioni") or "") >= today}
    if still_open and len(still_open & set(seen)) < len(still_open) / 2:
        sys.exit(f"toscana: only {len(still_open & set(seen))} of {len(still_open)} stored still-open records seen: "
                 "suspicious answer, nothing written")
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
    print(f"toscana: {len(items)} notices, {skipped} open of other types and {long_window} with a long window (dropped), "
          f"{national} national (dropped), {len(seen)} open, stored {len(procs)}", file=sys.stderr)


if __name__ == "__main__":
    run()
