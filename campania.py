"""Fetch the Regione Campania environmental procedures open for public observations: one request to the
region's public list (servizi-digitali.regione.campania.it), which shows every procedure since 2024 with its
observation deadline and an Aperta/Terminata flag. Keeps slim, person-free records like fetch.py.

Usage: python3 campania.py
Writes data/campania.json (dict id -> record). Refuses to run while data/BLOCKED-CAMPANIA exists.
"""
import datetime as dt
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request

from campania_comuni import COMUNI
from fetch import UA, scrub, iso, KEEP_CLOSED_DAYS

BASE = "https://servizi-digitali.regione.campania.it/Public/AccessoPubblico/ViaVas"
LIST = BASE + "/ElencoProgetti?Tipo=via"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "campania.json")
BLOCKED = os.path.join(HERE, "data", "BLOCKED-CAMPANIA")
AUTHORITY = "Regione Campania"
TYPES = {"VERVIA": "Verifica di Assoggettabilità a VIA", "VALPREL": "Valutazione preliminare (art. 6, c. 9)",
         "VEROTT": "Verifica di ottemperanza", "PRORVIA": "Proroga della VIA"}


def text(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def sentence(s):
    """The region writes most titles in capitals: lower them, keeping the first letter up."""
    letters = [c for c in s if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.7:
        s = s.lower()
        s = s[:1].upper() + s[1:]
    return s


def comune_key(c):
    """Upper case, with a final apostrophe as the accent it stands for: CARITA' -> CARITÀ."""
    return re.sub(r"([AEIOU])'(?=\s|$)", lambda m: {"A": "À", "E": "È", "I": "Ì", "O": "Ò", "U": "Ù"}[m[1]], c.upper())


def comune_name(c):
    """MARANO DI NAPOLI -> Marano di Napoli (ISTAT spelling); unknown names are capitalised word by word."""
    if comune_key(c) in COMUNI:
        return COMUNI[comune_key(c)][0]
    return " ".join(w[:1].upper() + w[1:] for w in c.lower().split())


def parse(h):
    body = h[h.find("<tbody"):h.find("</tbody>")]
    out = []
    for row in re.split(r"<tr>", body)[1:]:
        cells = {}
        for name, val in re.findall(r'<td[^>]*data-title="([^"]+)"[^>]*>(.*?)</td>', row, re.S):
            cells.setdefault(name, val)
        hidden = re.search(r'<td hidden data-title="PROGETTO">(.*?)</td>', row, re.S)
        doc = re.search(r'href="(/Public/AccessoPubblico/ViaVas/GetDocumentazione\?[^"]+)"', row)
        if "ID" not in cells or not doc:
            continue
        comuni = [text(x) for x in re.split(r"<br\s*/?>", cells.get("COMUNE", "")) if text(x)]
        out.append({"code": text(cells["ID"]), "proponente": text(cells.get("PROPONENTE", "")),
                    "title": text(hidden[1] if hidden else cells.get("PROGETTO", "")),
                    "comuni": comuni, "avvio": iso(text(cells.get("Data Avvio Procedimento", ""))),
                    "termine": iso(text(cells.get("Data Scadenza Osservazioni", ""))),
                    "consultazione": text(cells.get("Stato Consultazione", "")), "stato": text(cells.get("STATO", "")),
                    "url": "https://servizi-digitali.regione.campania.it" + html.unescape(doc[1])})
    return out


def record(x, old, today, now):
    num, _, kind = x["code"].partition("_")
    provs = sorted({COMUNI[comune_key(c)][1] for c in x["comuni"] if comune_key(c) in COMUNI})
    rec = {"id": f"campania-{num}-{kind.lower()}", "authority": AUTHORITY, "codice": x["code"], "url": x["url"],
           "procedure_type": TYPES.get(kind, kind), "tipologia": None, "title": sentence(x["title"]),
           "proponente": x["proponente"] or None, "regioni": ["Campania"], "province": provs,
           "comuni": [comune_name(c) for c in x["comuni"]], "avvio": x["avvio"],
           "termine_osservazioni": x["termine"], "termini": [x["termine"]], "stato": x["stato"] or None,
           "first_seen": old.get("first_seen", today), "last_seen": today, "closed_at": None,
           "updated_at": now if old.get("termine_osservazioni") != x["termine"] else old.get("updated_at", now)}
    return scrub(rec)


def run():
    if os.path.exists(BLOCKED):
        sys.exit(f"data/BLOCKED-CAMPANIA exists, not running: {open(BLOCKED).read().strip()}")
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = now[:10]
    procs = json.load(open(DATA, encoding="utf-8")) if os.path.exists(DATA) else {}
    req = urllib.request.Request(LIST, headers={"User-Agent": UA, "Accept-Language": "it"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            h = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            open(BLOCKED, "w").write(f"{now} HTTP {e.code}\n")
        sys.exit(f"campania: HTTP {e.code}")
    rows = parse(h)
    if len(rows) < 50:  # the list holds every procedure since 2024 (451 on 2026-10-27)
        sys.exit(f"campania: parsed only {len(rows)} rows: layout changed? nothing written")
    seen = {}
    for x in rows:
        if x["consultazione"] == "Aperta" and x["termine"] and x["termine"] >= today:
            rec = record(x, procs.get(f"campania-{x['code'].replace('_', '-').lower()}", {}), today, now)
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
    print(f"campania: {len(rows)} rows, {len(seen)} open, stored {len(procs)}", file=sys.stderr)


if __name__ == "__main__":
    run()
