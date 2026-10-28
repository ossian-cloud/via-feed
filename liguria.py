"""Fetch the Regione Liguria environmental procedures open for public observations: the region's SIRAVIAS list of
procedures in progress (siraviavas.regione.liguria.it, ElencoInCorsoVIA.aspx?page=N&Tipo=VIA; ~3 pages of 20 rows),
with the observation deadline in a column. At most MAX_REQUESTS requests per run, PAUSE seconds apart, stopping at
the first page with no rows or the last one the pager announces. The host has no robots.txt (404 on 2026-10-27); the main site's Crawl-delay 600 is
for www.regione.liguria.it, not for this host; it runs with the via fetch, about every 8 hours (services/site/run).

Rules:
- Included: rows whose "Termine per osservazioni" is today or later. Rows with a BLANK deadline are excluded
  (the phase is not open yet); a date written in the note as "scadenza <date>" is used when the column is blank.
- National procedures are excluded: the list also shows procedures whose authority is the State (MASE), which
  data/procedures.json already carries from fetch.py. The page labels each row with its "Tipo procedura"
  ("via nazionale", "verifica di assoggettabilità alla via nazionale"); a row is national when that label
  says "nazionale" or when its note points to the Ministry's site (mase / mite / "competenza nazionale").
  Matching by label is exact on this source, so no fuzzy match against the national records is needed.
- The "Responsabile" column (a named official with phone and e-mail) is never read.

Usage: python3 liguria.py
Writes data/liguria.json (dict id -> record). Refuses to run while data/BLOCKED-LIGURIA exists.
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

from fetch import UA, scrub, iso, personlike, KEEP_CLOSED_DAYS
from sigle import SIGLA_PROVINCE, SIGLA_REGION

LIST = "https://siraviavas.regione.liguria.it/ElencoInCorsoVIA.aspx?page={page}&Tipo=VIA"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "liguria.json")
BLOCKED = os.path.join(HERE, "data", "BLOCKED-LIGURIA")
AUTHORITY = "Regione Liguria"
PAUSE = 10
MAX_REQUESTS = 5
TYPES = {"via regionale": "Procedura di VIA regionale",
         "verifica di assoggettabilità alla via regionale": "Verifica di Assoggettabilità a VIA",
         "paur": "PAUR (VIA regionale, art. 27-bis)",
         "lista di controllo art. 6 c. 9": "Valutazione preliminare (art. 6, c. 9)",
         "via interregionale": "VIA interregionale"}
NATIONAL = re.compile(r"nazional|\bmase\b|\bmite\b|\bmatt\b|va\.mite\.gov", re.I)
SMALL = {"di", "del", "della", "delle", "dei", "degli", "e", "ed", "al", "alla", "in", "per", "da", "sul", "sulla"}
SUFFIX = [(r"\bS\.?R\.?L\.?(?=\W|$)", "S.r.l."), (r"\bS\.?P\.?A\.?(?=\W|$)", "S.p.a."), (r"\bS\.?N\.?C\.?(?=\W|$)", "S.n.c."),
          (r"\bS\.?A\.?S\.?(?=\W|$)", "S.a.s."), (r"\bR\.?F\.?I\.?(?=\W|$)", "RFI"), (r"\bEnac\b", "ENAC"),
          (r"\bAnas\b", "ANAS"), (r"\bAmiu\b", "AMIU"), (r"\bAcam\b", "ACAM")]


def text(s):
    s = re.sub(r"<br\s*/?>", " | ", s, flags=re.I)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s)).replace("\xa0", " ")).strip()


def fix_quotes(s):
    """The source turns quotes and apostrophes into '¿': between letters an apostrophe, else a quotation mark."""
    s = re.sub(r"(?<=\w)¿(?=\w)", "'", s)
    return re.sub(r"¿", '"', s)


def sentence(s, comuni=()):
    """The list writes titles all in lower case: give back the acronyms, company forms and the row's own comuni."""
    s = fix_quotes(s)
    s = re.sub(r"^\s*[\[(]?\s*id(?:_vip)?\s*:?\s*\d+\s*[\])]?\s*[-–:]?\s*", "", s, flags=re.I)  # '[id: 12495] ' prefix
    s = s.strip(" -–")
    s = re.sub(r"\b(assoggettabilit[àa] (?:a|alla)|(?:procedura|procedimento|valutazione|istanza) di|paur e|paur di) (via|vas)\b",
               lambda m: f"{m[1]} {m[2].upper()}", s, flags=re.I)
    s = re.sub(r"\b(paur|vinca|aia|arpal)\b", lambda m: m[1].upper() if m[1].lower() != "vinca" else "VIncA", s)
    s = re.sub(r"\bs\.\s?r\.\s?l\.", "S.r.l.", s)
    s = re.sub(r"\bs\.\s?p\.\s?a\.", "S.p.A.", s)
    for c in comuni:
        s = re.sub(r"\b(comun[ei] di |provincia di |a |in )" + re.escape(c.lower()) + r"\b", lambda m: m[1] + comune_name(c), s)
    return s[:1].upper() + s[1:]


def name(s):
    """'duferco sviluppo s.p.a' -> 'Duferco Sviluppo S.p.a.'; the source writes everything in lower case."""
    out = []
    for i, w in enumerate(fix_quotes(s).split()):
        out.append(w.lower() if i and w.lower() in SMALL else w[:1].upper() + w[1:].lower())
    s = " ".join(out)
    for pat, rep in SUFFIX:
        s = re.sub(pat, rep, s, flags=re.I)
    return s


def comune_name(c):
    """'prela'' -> Prelà, 'pieve di teco' -> Pieve di Teco."""
    accent = {"a": "à", "e": "è", "i": "ì", "o": "ò", "u": "ù"}
    c = re.sub(r"([aeiou])'$", lambda m: accent[m[1]], c.strip().lower())
    return " ".join(w if i and w in SMALL else w[:1].upper() + w[1:] for i, w in enumerate(c.split()))


def parse(h):
    h = h[h.find('class="searchResult"'):]
    h = h[:h.find("</table>")]
    out = []
    for row in re.split(r"<tr>", h)[1:]:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(cells) < 10:
            continue
        link = re.search(r'href="(https://docVia\.regione\.liguria\.it/[^"]+)"', cells[9], re.I)
        c = [text(x) for x in cells]
        out.append({"tipo": c[0].lower(), "avvio": iso(c[1]), "termine": iso(c[2]), "pratica": c[3], "title": c[4],
                    "proponente": c[5], "comuni": [x.strip() for x in c[7].split("|") if x.strip()],
                    "nota": c[8], "url": html.unescape(link[1]) if link else None})  # c[6] (Responsabile) is not read
    return out


MONTHS = "gennaio febbraio marzo aprile maggio giugno luglio agosto settembre ottobre novembre dicembre".split()
NOTE_DEADLINE = re.compile(r"\b(?:scadenz\w*|termine)\b[^.;]{0,30}?\b(\d{1,2})(?:\s+(" + "|".join(MONTHS) + r")\s+|[/.-](\d{1,2})[/.-])(\d{4})\b", re.I)


def note_deadline(nota):
    """The deadline the Region writes only in the note when the column is blank: "scadenza 20 novembre 2026" (pratica 1112, 28 Oct)."""
    m = NOTE_DEADLINE.search(nota)
    if not m:
        return None
    month = MONTHS.index(m[2].lower()) + 1 if m[2] else int(m[3])
    try:
        return dt.date(int(m[4]), month, int(m[1])).isoformat()
    except ValueError:
        return None


def is_national(x):
    return "nazionale" in x["tipo"] or bool(NATIONAL.search(x["nota"]))


def record(x, old, today, now):
    codes = [m.upper() for m in re.findall(r"\(\s*([A-Za-z]{2})\s*\)", x["title"]) if m.upper() in SIGLA_PROVINCE]
    pid = re.sub(r"[^a-z0-9]+", "-", x["pratica"].lower()).strip("-")
    rec = {"id": f"liguria-{pid}", "authority": AUTHORITY, "codice": x["pratica"], "url": x["url"] or LIST.format(page=1),
           "procedure_type": TYPES.get(x["tipo"], x["tipo"].capitalize()), "tipologia": None,
           "title": sentence(x["title"], x["comuni"]), "proponente": name(x["proponente"]) or None,
           "regioni": sorted({SIGLA_REGION[c] for c in codes} | {"Liguria"}), "province": [SIGLA_PROVINCE[c] for c in dict.fromkeys(codes)],
           "comuni": [comune_name(c) for c in x["comuni"]], "avvio": x["avvio"],
           "termine_osservazioni": x["termine"], "termini": [x["termine"]], "stato": None,
           "first_seen": old.get("first_seen", today), "last_seen": today, "closed_at": None,
           "updated_at": now if old.get("termine_osservazioni") != x["termine"] else old.get("updated_at", now)}
    if personlike(x["proponente"]):
        rec["proponente"] = None
    return scrub(rec)


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "it"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def run():
    if os.path.exists(BLOCKED):
        sys.exit(f"data/BLOCKED-LIGURIA exists, not running: {open(BLOCKED).read().strip()}")
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = now[:10]
    procs = json.load(open(DATA, encoding="utf-8")) if os.path.exists(DATA) else {}
    seen, rows, national, blank = {}, [], 0, 0
    for page in range(1, MAX_REQUESTS + 1):
        url = LIST.format(page=page)
        try:
            h = get(url)
            got = parse(h)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                open(BLOCKED, "w").write(f"{now} HTTP {e.code} on {url}\n")
            sys.exit(f"liguria: HTTP {e.code} on {url}")
        if not got:
            break
        rows += got
        last = re.search(r"pagina\s+\d+\s+di\s+(\d+)", h)
        if last and page >= int(last[1]):  # the page says it is the last one: no request for an empty page
            break
        if page < MAX_REQUESTS:
            time.sleep(PAUSE)
    if len(rows) < 15:  # 44 rows on 2026-10-27
        sys.exit(f"liguria: parsed only {len(rows)} rows: layout changed? nothing written")
    for x in rows:
        x["termine"] = x["termine"] or note_deadline(x["nota"])
        if is_national(x):
            national += 1
        elif not x["termine"]:
            blank += 1
        elif x["termine"] >= today and x["pratica"]:
            rec = record(x, procs.get("liguria-" + re.sub(r"[^a-z0-9]+", "-", x["pratica"].lower()).strip("-"), {}), today, now)
            seen[rec["id"]] = rec
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
    print(f"liguria: {len(rows)} rows, {national} national (dropped), {blank} regional without deadline (dropped), "
          f"{len(seen)} open, stored {len(procs)}", file=sys.stderr)


if __name__ == "__main__":
    run()
