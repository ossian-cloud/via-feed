"""Fetch the Regione Sardegna environmental procedures open for public observations from the SIRA news page of the
Servizio V.I.A. (portal.sardegnasira.it/news-detail-valutazioni-ambientali): one Liferay news block per procedure, in free
text ("... Entro (30) trenta giorni dalla pubblicazione della documentazione (02.10.2026), il pubblico interessato può
presentare osservazioni ..."). ONE request per run (the page, ~600 KB). robots.txt allowed everything on 2026-10-27 and
2026-10-28 (checked by hand; the fetcher does not read it again each run: if the site starts blocking it answers 403/429 and
data/BLOCKED-SARDEGNA is written, which stops all further runs).

There is no deadline field, so the deadline is COMPUTED: publication date + the number of days the text states.
- Publication date: the date in brackets after "pubblicazione della documentazione (DD.MM.YYYY)", or, when the text says
  "pubblicazione del suddetto avviso", the "in data DD.MM.YYYY" of "comunica/informa che ... è stato pubblicato in data ...".
- Days: "Entro trenta (30) giorni" / "Entro (30) trenta giorni" / "Entro quindici (15) giorni"; the number and the word
  must agree. No number, no date, or disagreement: the block is skipped, never guessed.
- Deadline = publication date + days (the publication day itself is not counted). It is NOT moved when it falls on a
  Saturday, Sunday or Italian public holiday: the record carries "termine_calcolato": true (always) and, when it does fall
  on one, "termine_festivo" (a word for the day); build.py tells the reader the date is computed and the avviso counts.
- Only the part of a block before "Pubblicazioni precedenti" is read: older notices of the same procedure are history.
- Blocks without that sentence (deposits, integrations, outcomes, "interruzione") are not consultations open by their own text: skipped.
- Included: computed deadline today or later.

Ids: "sardegna-via-<N>" / "sardegna-vinca-<N>" with N the idOst of the procedure's own page on the portal
("dettaglio-progetti-via?idOst=N" or "dettaglio-progetti-vinca?idOst=N", the "Consulta i documenti e la scheda del
procedimento" link of the block). It is the Region's own procedure number, stable across new notices of the same procedure
(the news-article slugs gain a "-1", "-2" suffix at each new publication, so they are not used). Without that link the
block is skipped. If two blocks share an id, the later deadline wins.

Comune and province come from the title only when it says "comune di X" / "a X (SS)"; proponent only from "La Società X ha
depositato" / "Il Comune di X ha depositato" and the like. Otherwise empty. Proponents that look like natural persons are
dropped (fetch.personlike / scrub); no name of an official or technician is ever read (the text is not stored).

Usage: python3 sardegna.py
Writes data/sardegna.json (dict id -> record). Refuses to run while data/BLOCKED-SARDEGNA exists.
"""
import datetime as dt
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request

from fetch import UA, scrub, personlike, KEEP_CLOSED_DAYS
from sigle import SIGLA_PROVINCE

PAGE = "https://portal.sardegnasira.it/news-detail-valutazioni-ambientali"
DETAIL = "https://portal.sardegnasira.it/web/sardegnaambiente/dettaglio-progetti-{kind}?idOst={n}"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "sardegna.json")
BLOCKED = os.path.join(HERE, "data", "BLOCKED-SARDEGNA")
AUTHORITY = "Regione Sardegna"
MIN_BLOCKS = 20  # 53-75 news blocks on 2026-10-28
MIN_ITEMS = 10  # 37 with a computable deadline on 2026-10-28: fewer means the wording changed, and closing everything would be wrong
NUMBERS = {"cinque": 5, "dieci": 10, "quindici": 15, "venti": 20, "trenta": 30, "quarantacinque": 45, "sessanta": 60,
           "novanta": 90}
DATE = r"(\d{2})\.(\d{2})\.(\d{4})"
HISTORY = re.compile(r"\b(?:Pubblicazion[ei] precedent[ei]|Precedent[ei] pubblicazion[ei])\b", re.I)
DAYS = re.compile(r"\bEntro\s+(?:\(\s*(\d+)\s*\)\s+([a-zà-ÿ]+)|([a-zà-ÿ]+)\s+\(\s*(\d+)\s*\)|(\d+))\s+giorni\s+dalla\s+"
                  r"pubblicazione\s+(?:della\s+documentazione|del\s+suddetto\s+avviso)", re.I)
PUB_DOC = re.compile(r"pubblicazione\s+della\s+documentazione\s*\(\s*" + DATE)
PUB_AVVISO = re.compile(r"(?:comunica|informa)\s+che\s*,?\s+(?:.{0,300}?\bpubblicato,?\s+)?in\s+data\s+" + DATE)
SMALL = r"(?:di|del|della|dei|degli|delle|de|sa|su|s')"
NAME = r"[A-ZÀ-Ý][\wà-ÿ'’]*(?:\s+(?:" + SMALL + r"\s+)?[A-ZÀ-Ý][\wà-ÿ'’]*)*"
TITLE_COMUNE = re.compile(r"\b[Cc]omune\s+di\s+(" + NAME + r")")
TITLE_PROV = re.compile(r"\b(?:a|ad)\s+(" + NAME + r")\s*\(\s*([A-Z]{2})\s*\)")
PROPONENT = [re.compile(r"(?:^|\.\s)(?:La\s+)?Societ[àa]\s+(.+?)\s*,?\s+ha\s+(?:depositato|trasmesso)"),
             re.compile(r"da\s+parte\s+della\s+Societ[àa]\s+(.+?)\s*,\s+è\s+stato"),
             re.compile(r"(?:^|\.\s)(?:Il\s+)?(Comune\s+di\s+" + NAME + r")\s*,?\s+ha\s+depositato"),
             re.compile(r"(?:^|\.\s)(?:La\s+)?(Città\s+Metropolitana\s+di\s+" + NAME + r")\s*,?\s+ha\s+depositato")]


def ws(s):
    return re.sub(r"\s+", " ", html.unescape(s).replace("\xa0", " ")).strip()


def text(h):
    return ws(re.sub(r"<[^>]+>", " ", re.sub(r"(?s)<(script|style).*?</\1>", "", h)))


def easter(y):
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mo, da = divmod(h + l - 7 * m + 114, 31)
    return dt.date(y, mo, da + 1)


def non_working(d):
    """'sabato' / 'domenica' / 'festivo' for a Saturday, a Sunday or an Italian public holiday (national ones; the
    Sardinian 28 April, Sa die de sa Sardigna, is a regional day and is not counted), else None."""
    if d.weekday() == 5:
        return "sabato"
    if d.weekday() == 6:
        return "domenica"
    fixed = {(1, 1), (6, 1), (25, 4), (1, 5), (2, 6), (15, 8), (1, 11), (8, 12), (25, 12), (26, 12)}
    if (d.day, d.month) in fixed or d == easter(d.year) + dt.timedelta(days=1):
        return "festivo"
    return None


def days_stated(m):
    """(number, word) in any of the three spellings; None if the number and the word disagree."""
    if m[1]:
        n, w = int(m[1]), m[2].lower()
    elif m[3]:
        n, w = int(m[4]), m[3].lower()
    else:
        return int(m[5])
    return n if NUMBERS.get(w) == n else None


def to_iso(m):
    try:
        return dt.date(int(m[3]), int(m[2]), int(m[1])).isoformat()
    except ValueError:
        return None


def title_places(title):
    comuni, codes = [], []
    m = TITLE_COMUNE.search(title)
    if m:
        comuni.append(m[1].strip())
    m = TITLE_PROV.search(title)
    if m and m[2] in SIGLA_PROVINCE:
        codes.append(m[2])
        if not comuni:
            comuni.append(m[1].strip())
    return comuni, codes


def proponent(body):
    for rx in PROPONENT:
        m = rx.search(body)
        if m:
            p = ws(m[1]).strip(" ,")
            return p if 3 <= len(p) <= 80 else None
    return None


def type_of(title, body):
    t = title.lower()
    if "valutazione appropriata" in body.lower() or "v.inc.a" in body.lower():
        return "Valutazione di incidenza (VIncA, livello II)"
    if "preliminare" in t:
        return "Valutazione preliminare (art. 6, c. 9)"
    if t.startswith("verifica") or "verifica di assoggettabilità" in body.lower():
        return "Verifica di Assoggettabilità a VIA"
    if re.match(r"p\.?a\.?u\.?r", t) or re.search(r"p\.?a\.?u\.?r\b|provvedimento unico regionale", body, re.I):
        return "PAUR (VIA regionale, art. 27-bis)"
    if t.startswith("via"):
        return "Procedura di VIA regionale"
    return "Procedura regionale (Servizio V.I.A.)"


EVENT = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\s*[-–]\s*([^\n]{0,80}?)\s+https?://")  # "28.08.2026 - Interruzione consultazione pubblica https://..."
STOPPED = re.compile(r"\b(?:interruzion\w*|sospension\w*|sospes[oa]|revoc\w*|archiviazion\w*|ritirat[oa]|annullat[oa])\b", re.I)


def parse(h):
    """-> (n blocks seen, list of dicts) one per news block that states a consultation with a computable deadline.
    The page lists news newest first. Guards found by review on 28 Oct:
    - a block can link the wrong procedure: Carta's "chiosco reception" (27.10) has two identical blocks, one linking
      idOst 157132, which is really Fiume Santo's centro balneare. An idOst linked from blocks with different titles is
      "ambiguous": its record links the news page, not a detail page. Identical blocks (title, date, days) are kept
      once, preferring an unambiguous id, then the highest.
    - when the "Precedenti pubblicazioni" history of any block for the same idOst lists an interruption (suspension,
      revocation, withdrawal) dated on or after a notice, that notice is out (procedure 91761: 24.08 notice, 28.08
      "Interruzione consultazione pubblica", 07.09 new notice)."""
    parts = re.split(r'<div class="news-detail">', h)[1:]
    blocks, out, skipped, seen = [], [], 0, 0
    for blk in parts:
        t = re.search(r"(?s)<h2[^>]*>(.*?)</h2>", blk)
        if not t or "news-detail-body" not in blk:
            continue
        seen += 1
        i = blk.find("news-detail-body")
        title = ws(re.sub(r"<[^>]+>", " ", t[1]))
        full = text(blk[blk.find(">", i) + 1:])
        body = HISTORY.split(full, 1)[0]
        stops = [f"{y}-{m}-{d}" for d, m, y, what in EVENT.findall(full) if STOPPED.search(what)]
        link = re.search(r'href="[^"]*dettaglio-progetti-(via|vinca)\?idOst=(\d+)"', blk)  # the block's own procedure
        ids = {link[2]} if link else set()  # later links can be strays: Banari's block also links Bultei's 159458
        blocks.append((title, stops, ids))
        dm = DAYS.search(body)
        if not (link and dm):
            continue  # not a consultation by its own text (deposit, integration, outcome...) or no procedure page
        n = days_stated(dm)
        pm = PUB_DOC.search(body) if "documentazione" in dm[0].lower() else PUB_AVVISO.search(body)
        pub = to_iso(pm) if pm else None
        if not (n and pub):
            skipped += 1
            continue
        if any(link[2] in ids2 and day >= pub for _, stops2, ids2 in blocks for day in stops2):
            skipped += 1
            continue  # a block's history says the consultation was interrupted (or the like) on or after this notice
        out.append({"kind": link[1], "n": link[2], "title": title, "body": body, "avvio": pub, "giorni": n})
    titles = {}
    for title, _, ids in blocks:
        for n in ids:
            titles.setdefault(n, set()).add(re.sub(r"^(?:verifica(?: ex post)?|paur|via|vinca)\s*[-–—]\s*", "", title.lower()).strip(" ."))
    for x in out:
        x["ambiguous"] = len(titles.get(x["n"], ())) > 1
    best = {}
    for x in out:
        key = (x["title"].lower(), x["avvio"], x["giorni"])
        rank = lambda y: (y["ambiguous"], -int(y["n"]))  # unambiguous first, then the highest id
        if key not in best or rank(x) < rank(best[key]):
            best[key] = x
    return seen, list(best.values()), skipped


def record(x, old, today, now):
    end = dt.date.fromisoformat(x["avvio"]) + dt.timedelta(days=x["giorni"])
    comuni, codes = title_places(x["title"])
    prop = proponent(x["body"])
    rec = {"id": f"sardegna-{x['kind']}-{x['n']}", "authority": AUTHORITY, "codice": f"idOst {x['n']}",
           "url": PAGE if x.get("ambiguous") else DETAIL.format(kind=x["kind"], n=x["n"]), "procedure_type": type_of(x["title"], x["body"]), "tipologia": None,
           "title": x["title"], "proponente": None if not prop or personlike(prop) else prop,
           "regioni": ["Sardegna"], "province": [SIGLA_PROVINCE[c] for c in codes], "comuni": comuni,
           "avvio": x["avvio"], "termine_osservazioni": end.isoformat(), "termini": [end.isoformat()],
           "termine_calcolato": True, "giorni": x["giorni"], "stato": None,
           "first_seen": old.get("first_seen", today), "last_seen": today, "closed_at": None,
           "updated_at": now if old.get("termine_osservazioni") != end.isoformat() else old.get("updated_at", now)}
    if non_working(end):
        rec["termine_festivo"] = non_working(end)
    return scrub(rec)


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "it"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def run(src=None):
    if os.path.exists(BLOCKED):
        sys.exit(f"data/BLOCKED-SARDEGNA exists, not running: {open(BLOCKED).read().strip()}")
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = now[:10]
    procs = json.load(open(DATA, encoding="utf-8")) if os.path.exists(DATA) else {}
    try:
        h = open(src, encoding="utf-8").read() if src else get(PAGE)
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            open(BLOCKED, "w").write(f"{now} HTTP {e.code} on {PAGE}\n")
        sys.exit(f"sardegna: HTTP {e.code} on {PAGE}")
    seen_blocks, items, skipped = parse(h)
    if seen_blocks < MIN_BLOCKS or len(items) < MIN_ITEMS:
        sys.exit(f"sardegna: parsed only {seen_blocks} news blocks, {len(items)} with a deadline: layout or wording changed? nothing written")
    seen = {}
    for x in items:
        rec = record(x, procs.get(f"sardegna-{x['kind']}-{x['n']}", {}), today, now)
        if rec["termine_osservazioni"] < today:
            continue
        if rec["id"] not in seen or rec["termine_osservazioni"] > seen[rec["id"]]["termine_osservazioni"]:
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
    print(f"sardegna: {seen_blocks} news blocks, {len(items)} with a computable deadline ({skipped} skipped: no usable "
          f"date or days), {len(seen)} open, stored {len(procs)}, 1 request", file=sys.stderr)


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else None)
