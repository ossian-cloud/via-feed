"""Fetch the Regione Autonoma Valle d'Aosta VIA procedures open for public observations from the Region's own list
"Archivio - Procedure Via" (www.regione.vda.it/territorio/ambiente/valutazioniambientali/via/procedure_via_archivio_i.asp):
one static HTML table, one row per project, with a column "Termine ultimo presentazione Osservazioni" (dd/mm/yyyy), and
for each row a detail page (via_dettaglio_i.asp?pk=N) that adds the proponent and the filing date.
robots.txt of www.regione.vda.it (checked by hand 2026-10-28) disallows only /turismo/, some single PDFs and a few other
paths; /territorio/ambiente/ is allowed and there is no Crawl-delay. The fetcher does not read robots.txt each run: if the
site starts blocking it answers 403/429 and data/BLOCKED-VALLEDAOSTA is written, which stops all further runs.

Requests per run: the list (1) + one detail page per OPEN row (at most MAX_DETAILS), PAUSE seconds apart. A detail page that
fails is skipped (the record keeps the list's data and no proponent); it never stops the run.

Rules:
- Included: rows whose deadline is today or later. Rows without a readable deadline are excluded.
- The page decodes as Windows-1252, not UTF-8.
- Title: the "Intervento" text of the list (what the project is) + " - località, comune"; the list has no other title.
- Link: the row's own detail page; the list page if a row has no link.
- Proponent: from the detail page ("Soc. X s.r.l. con sede ad Aosta" -> "Soc. X s.r.l."); dropped when it looks like a natural
  person (fetch.personlike / scrub). Officials' names on the Region's pages are never read.
- Valle d'Aosta has no provinces: "province" is empty, "regioni" is ["Valle d'Aosta"].

Usage: python3 valledaosta.py
Writes data/valledaosta.json (dict id -> record). Refuses to run while data/BLOCKED-VALLEDAOSTA exists.
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

BASE = "https://www.regione.vda.it/territorio/ambiente/valutazioniambientali/via/"
LIST = BASE + "procedure_via_archivio_i.asp"
DETAIL = BASE + "via_dettaglio_i.asp?pk={pk}"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "valledaosta.json")
BLOCKED = os.path.join(HERE, "data", "BLOCKED-VALLEDAOSTA")
AUTHORITY = "Regione Autonoma Valle d'Aosta"
PROCEDURE_TYPE = "Procedura di VIA regionale"
PAUSE = 10
MAX_DETAILS = 8


def text(s):
    s = re.sub(r"<br\s*/?>", " ", s, flags=re.I)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s)).replace("\xa0", " ")).strip()


def parse(h):
    """Rows of the table that follows the 'Termine ultimo' header: comune, località, intervento, iter, deadline, pk."""
    i = h.find("Termine ultimo")
    if i < 0:
        return None
    h = h[i:]
    h = h[:h.find("</table>")]
    out = []
    for row in re.split(r"<tr[ >]", h)[1:]:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(cells) < 5:
            continue
        pk = re.search(r"via_dettaglio_i\.asp\?pk=(\d+)", row)
        c = [text(x) for x in cells]
        out.append({"comune": c[0], "localita": c[1], "intervento": c[2], "iter": c[3], "termine": iso(c[4]),
                    "pk": pk[1] if pk else None})
    return out


def parse_detail(h):
    """{'proponente', 'avvio', 'numero'} from the detail page; missing pieces are simply absent."""
    out = {}
    m = re.search(r"<b>Proponente:?</b>\s*:?\s*<br\s*/?>(.*?)</p>", h, re.S | re.I)
    if m:
        p = re.split(r"\s+con sede\b", text(m[1]), maxsplit=1, flags=re.I)[0].strip(" ,;")
        if p and not personlike(p):
            out["proponente"] = p
    m = re.search(r"<b>Data presentazione istanza:?</b>.*?<br\s*/?>(.*?)</p>", h, re.S | re.I)
    if m and iso(text(m[1])):
        out["avvio"] = iso(text(m[1]))
    m = re.search(r"<h1[^>]*>\s*Progetto n\.\s*([\d/]+)\s*</h1>", h, re.I)
    if m:
        out["numero"] = m[1]
    return out


def title_of(x):
    what = x["intervento"].strip(" .")
    what = what[:1].upper() + what[1:]
    where = ", ".join(b for b in (x["localita"], x["comune"]) if b)
    return f"{what} - {where}" if where else what


def record(x, det, old, today, now):
    rec = {"id": f"valledaosta-{x['pk']}", "authority": AUTHORITY, "codice": det.get("numero") and "Progetto n. " + det["numero"],
           "url": DETAIL.format(pk=x["pk"]) if x["pk"] else LIST, "procedure_type": PROCEDURE_TYPE,
           "tipologia": x["intervento"], "title": title_of(x), "proponente": det.get("proponente") or old.get("proponente"),
           "regioni": ["Valle d'Aosta"], "province": [], "comuni": [x["comune"]] if x["comune"] else [],
           "avvio": det.get("avvio") or old.get("avvio"), "termine_osservazioni": x["termine"], "termini": [x["termine"]],
           "stato": None, "first_seen": old.get("first_seen", today), "last_seen": today, "closed_at": None,
           "updated_at": now if old.get("termine_osservazioni") != x["termine"] else old.get("updated_at", now)}
    return scrub(rec)


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "it"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("cp1252", "replace")


def blocked(e, url, now):
    if e.code in (403, 429):
        open(BLOCKED, "w").write(f"{now} HTTP {e.code} on {url}\n")


def run():
    if os.path.exists(BLOCKED):
        sys.exit(f"data/BLOCKED-VALLEDAOSTA exists, not running: {open(BLOCKED).read().strip()}")
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = now[:10]
    procs = json.load(open(DATA, encoding="utf-8")) if os.path.exists(DATA) else {}
    try:
        rows = parse(get(LIST))
    except urllib.error.HTTPError as e:
        blocked(e, LIST, now)
        sys.exit(f"valledaosta: HTTP {e.code} on {LIST}")
    if rows is None:  # no table header: an error page or a new layout. An empty table is fine (nothing open).
        sys.exit("valledaosta: table header not found: layout changed? nothing written")
    seen, nodate, details = {}, 0, 0
    for x in rows:
        if not x["termine"]:
            nodate += 1
        elif x["termine"] >= today and x["pk"]:
            det = {}
            if details < MAX_DETAILS:
                if details:
                    time.sleep(PAUSE)
                details += 1
                try:
                    det = parse_detail(get(DETAIL.format(pk=x["pk"])))
                except urllib.error.HTTPError as e:
                    blocked(e, DETAIL.format(pk=x["pk"]), now)
                    if e.code in (403, 429):
                        sys.exit(f"valledaosta: HTTP {e.code} on a detail page")
                except (urllib.error.URLError, TimeoutError):
                    pass
            rec = record(x, det, procs.get("valledaosta-" + x["pk"], {}), today, now)
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
    print(f"valledaosta: {len(rows)} rows, {nodate} without deadline (dropped), {len(seen)} open, stored {len(procs)}", file=sys.stderr)


if __name__ == "__main__":
    run()
