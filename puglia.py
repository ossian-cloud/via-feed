"""Fetch the Regione Puglia environmental procedures open for public observations from the Region's "Puglia.con"
publication component (pugliacon.regione.puglia.it/comp_pub, the public page "ricercaInPubblicita"), through the same
unauthenticated JSON endpoints that page calls (/comp_pubb_service/public/...):
  1. tipoprocedura/getConcludedInProgressProcedures?famigliaid=...  per family (VIA, VINCA, AIA): the procedure types
     that have procedures in progress ("inProgres" > 0);
  2. procedura/searchForType (POST)  per such type: the procedures in progress;
  3. procedura/findById  per OPEN procedure: the comuni and provinces (not in the list item) and the flag attivaOsservazioni.
robots.txt of the host (checked by hand 2026-10-28) allows everything. The fetcher does not read it each run: if the site
starts blocking it answers 403/429 and data/BLOCKED-PUGLIA is written, which stops all further runs.

Requests per run: 3 (families) + one per type with procedures in progress (~4) + one findById per open procedure
(~13), PAUSE seconds apart; at most MAX_REQUESTS. About 20 per run.

Rules:
- The deadline is the field "scadenzaOsservazioni" of the procedure: the end of the period for observations, a UTC
  timestamp, converted to the Rome calendar date. This is the same field the portal's own detail page uses to show its
  "osservazioni" button (together with "avvioOsservazioni" and the flag "attivaOsservazioni"). Included: items whose
  date is today or later AND whose findById says attivaOsservazioni is true. Items without that field (e.g. the VAS ones,
  "inConsultazione" without a date, 2022-23) are excluded; VAS is not requested at all.
- VIncA (valutazione di incidenza) items are included when they have that deadline, and the procedure type says so
  ("Screening di incidenza (VIncA)", "Valutazione appropriata di incidenza (VIncA)"). Many are small works (e.g. tree plantations).
- Title: the project's own description ("nome"), whitespace cleaned, a trailing "Proponente: <name>" cut off (often a person); comuni and provinces are separate fields (findById).
- Proponent: the portal writes "<P.IVA> NAME": the leading number is removed, and the name is dropped when it looks like
  a natural person (fetch.personlike / scrub): many small VIncA proponents are individuals.
- Link: the procedure's own page in the Region's portal, /comp_pub/dettaglioProcedure/<id>/0 (an Angular route of the same
  app; the server answers its shell page for every route; the route and the tab number 0 = "dettaglio" are in the app's code).
- Provinces: findById "provinceProcedura" (sigla), "regioni" from them plus Puglia.
- Any request that fails (or a type list that does not parse) stops the run with nothing written: otherwise the missing
  items would be marked closed.

Usage: python3 puglia.py
Writes data/puglia.json (dict id -> record). Refuses to run while data/BLOCKED-PUGLIA exists.
"""
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

from fetch import UA, scrub, personlike, KEEP_CLOSED_DAYS
from sigle import SIGLA_PROVINCE, SIGLA_REGION

SERVICE = "https://pugliacon.regione.puglia.it/comp_pubb_service/public/"
PAGE = "https://pugliacon.regione.puglia.it/comp_pub/dettaglioProcedure/{id}/0"
LIST_PAGE = "https://pugliacon.regione.puglia.it/comp_pub/ricercaInPubblicita/true"
FAMILIES = {"VIA": "e123ec40-b753-11e9-a2a3-2a2ae2dbcce4", "VINCA": "e123ed8a-b753-11e9-a2a3-2a2ae2dbcce4",
            "AIA": "e123eec0-b753-11e9-a2a3-2a2ae2dbcce4"}
IN_PROGRESS = "e123e9de-b753-22e9-a2a3-2a2ae2dbcce4"  # the portal's id for "procedura in corso"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "puglia.json")
BLOCKED = os.path.join(HERE, "data", "BLOCKED-PUGLIA")
AUTHORITY = "Regione Puglia"
PAUSE = 3
MAX_REQUESTS = 45
TYPES = {"PAUR: art.27 bis": "PAUR (VIA regionale, art. 27-bis)",
         "Verifica di Assoggettabilità VIA": "Verifica di Assoggettabilità a VIA",
         "VIA": "Procedura di VIA regionale",
         "Screening": "Screening di incidenza (VIncA)",
         "Valutazione appropriata": "Valutazione appropriata di incidenza (VIncA)"}
ROME = ZoneInfo("Europe/Rome")


class Run:
    def __init__(self):
        self.n = 0

    def call(self, url, body=None):
        if self.n >= MAX_REQUESTS:
            sys.exit(f"puglia: more than {MAX_REQUESTS} requests needed, nothing written")
        if self.n:
            time.sleep(PAUSE)
        self.n += 1
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, "Accept": "application/json",
                                                              **({"Content-Type": "application/json"} if data else {})})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                open(BLOCKED, "w").write(f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%M:%SZ} HTTP {e.code} on {url}\n")
            sys.exit(f"puglia: HTTP {e.code} on {url}")
        except (urllib.error.URLError, ValueError, TimeoutError) as e:
            sys.exit(f"puglia: {e} on {url}: nothing written")
        if d.get("codiceEsito") != "OK":
            sys.exit(f"puglia: {d.get('codiceEsito')} on {url}: nothing written")
        return d["payload"]


def deadline(ts):
    """'2026-10-29T12:26:35.819+0000' -> '2026-10-29' (Rome date)."""
    if not ts:
        return None
    try:
        t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t.astimezone(ROME).date().isoformat()


PERSON_REF = re.compile(r"\b(?i:sig\.?(?:ra|\.ra)?|dott\.?(?:ssa)?|ing\.?|arch\.?|geom\.?|ditta)\s+(?:[A-ZÀ-Ý][\wÀ-ÿ']+)(?:\s+(?:[A-ZÀ-Ý][\wÀ-ÿ']+)){0,2}")


def clean_title(s, dropped=None):
    """dropped: the raw proponent when it was dropped as a person; its name in the title becomes "la ditta"."""
    s = re.sub(r"\s+", " ", s or "").strip()
    s = re.sub(r"\s*\bProponente\s*:.*$", "", s, flags=re.I)  # some titles end with the proponent's name (often a person)
    if dropped and len(dropped.strip()) > 3:
        s = re.sub(r"(?<![\wÀ-ÿ])" + r"\s+".join(re.escape(w) for w in dropped.split()) + r"(?![\wÀ-ÿ])", "la ditta", s, flags=re.I)
    s = PERSON_REF.sub("la ditta", s)
    s = re.sub(r"\b(del|dal|al)\s+la ditta", lambda m: {"del": "della", "dal": "dalla", "al": "alla"}[m[1].lower()] + " ditta", s)
    s = re.sub(r"\b(DEFINITIVO|PRELIMINARE|ESECUTIVO|FATTIBILIT[AÀ])(?=[a-z])", r"\1 ", s)  # "DEFINITIVOdi" in the source
    return s


def raw_proponent(s):
    """The proponent as the portal writes it, without the leading P.IVA / fiscal code."""
    return re.sub(r"^\s*(?:\d{11}|[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z])\s+", "", s or "").strip()


def proponent(s):
    s = re.sub(r"SOCIET[AÀ]['’]? A RESPONSABILIT[AÀ]['’]? LIMITATA", "S.r.l.", s or "", flags=re.I)  # "responsabil" would trip scrub()
    s = re.sub(r"^\s*(?:\d{11}|[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z])\s+", "", s or "").strip()
    return None if not s or personlike(s) else s


def record(x, det, old, today, now):
    sigle = [p["value"] for p in det.get("provinceProcedura") or [] if p.get("value") in SIGLA_PROVINCE]
    if not sigle and old:
        sigle = [k for k, v in SIGLA_PROVINCE.items() if v in old.get("province", [])]
    comuni = [c["nome_comune"].strip() for c in det.get("comuniprocedura") or [] if c.get("nome_comune")] or old.get("comuni") or []
    d = deadline(x["scadenzaOsservazioni"])
    start = deadline(x.get("avvioOsservazioni"))
    avvio = start  # the portal's own "Data avvio consultazioni" (checked in the browser 28 Oct); dataPubblicazione is not shown there, so not used
    prop = proponent(x.get("proponente"))
    dropped = raw_proponent(x.get("proponente")) if x.get("proponente") and not prop else None
    rec = {"id": "puglia-" + x["id"], "authority": AUTHORITY, "codice": x.get("codice"), "url": PAGE.format(id=x["id"]),
           "procedure_type": TYPES.get(x["nomeTipoProcedura"], x["nomeTipoProcedura"]), "tipologia": None,
           "title": clean_title(x["nome"], dropped), "proponente": prop,
           "regioni": sorted({SIGLA_REGION[c] for c in sigle} | {"Puglia"}), "province": [SIGLA_PROVINCE[c] for c in dict.fromkeys(sigle)],
           "comuni": comuni, "avvio": avvio,
           "termine_osservazioni": d, "termini": [d], "stato": None,
           "first_seen": old.get("first_seen", today), "last_seen": today, "closed_at": None,
           "updated_at": now if old.get("termine_osservazioni") != d else old.get("updated_at", now)}
    return scrub(rec)


def run():
    if os.path.exists(BLOCKED):
        sys.exit(f"data/BLOCKED-PUGLIA exists, not running: {open(BLOCKED).read().strip()}")
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = dt.datetime.now(ROME).date().isoformat()
    procs = json.load(open(DATA, encoding="utf-8")) if os.path.exists(DATA) else {}
    r = Run()
    types, expected = [], {}
    for fam, fid in FAMILIES.items():
        got_types = r.call(SERVICE + f"tipoprocedura/getConcludedInProgressProcedures?famigliaid={fid}&inPubblicita=true")
        if not isinstance(got_types, list):
            sys.exit(f"puglia: type list of {fam} is not a list: nothing written")
        for t in got_types:
            n = t.get("inProgres", 0)
            if not isinstance(n, int):
                sys.exit(f"puglia: inProgres of {t.get('id')} is not a number: nothing written")
            if n > 0:
                types.append(t["id"])
                expected[t["id"]] = n
    stored_open = {k for k, v in procs.items() if not v.get("closed_at")}
    if not types and stored_open:
        sys.exit("puglia: no type in progress but open records are stored: nothing written")
    items = []
    for tid in types:
        body = {"autoritaCompetente": "", "autoritaProcedente": "", "codice": None, "codiceLike": True, "column": "DENOMINAZIONE",
                "direction": "ASC", "page": 1, "limit": 100, "draft": False, "customFieldList": None, "dataAvvioDa": "",
                "dataAvvioA": "", "dataFineDa": "", "dataFineA": "", "idTipoprocedura": tid, "nome": "", "nome_comune": None,
                "procedura_conclusa": IN_PROGRESS, "proponente": "", "testo": "", "idFamiglia": None, "inPubblicita": True,
                "provincia": None, "nomeProvincia": None}
        got = r.call(SERVICE + "procedura/searchForType", body)["procedure"]["list"]
        if len(got) < expected[tid]:
            sys.exit(f"puglia: type {tid}: {len(got)} procedures listed, the portal counts {expected[tid]} in progress "
                     "(limit 100 or changed answer): nothing written")
        items += [x for x in got if not x.get("deleted") and not x.get("draft")]
    open_items = [x for x in items if (deadline(x.get("scadenzaOsservazioni")) or "") >= today]
    seen = {}
    for x in open_items:
        old = procs.get("puglia-" + x["id"], {})
        det = r.call(SERVICE + f"procedura/findById?id={x['id']}&limit=5")  # always: attivaOsservazioni and the province
        if not det.get("attivaOsservazioni"):
            continue
        rec = record(x, det, old, today, now)
        seen[rec["id"]] = rec
    still_open = {k for k in stored_open if (procs[k].get("termine_osservazioni") or "") >= today}
    if stored_open and not items:
        sys.exit("puglia: nothing parsed but open records are stored: nothing written")
    if still_open and len(still_open & set(seen)) < len(still_open) / 2:
        sys.exit(f"puglia: only {len(still_open & set(seen))} of {len(still_open)} stored still-open records seen: "
                 "suspicious answer, nothing written")
    count = {}
    for rec in seen.values():
        count[rec["title"]] = count.get(rec["title"], 0) + 1
    for rec in seen.values():
        if count[rec["title"]] > 1 and rec.get("codice"):
            rec["title"] += f" (codice {rec['codice']})"
    for pid, rec in procs.items():
        if pid not in seen and not rec.get("closed_at"):
            rec["closed_at"] = today
    procs.update(seen)
    limit = (dt.date.fromisoformat(today) - dt.timedelta(days=KEEP_CLOSED_DAYS)).isoformat()
    procs = {k: v for k, v in procs.items() if not v.get("closed_at") or v["closed_at"] >= limit}
    os.makedirs(os.path.dirname(DATA), exist_ok=True)
    with open(DATA + ".tmp", "w", encoding="utf-8") as f:
        json.dump(procs, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(DATA + ".tmp", DATA)
    print(f"puglia: {len(items)} in progress, {len(open_items)} with deadline >= today, {len(seen)} open, "
          f"stored {len(procs)}, {r.n} requests", file=sys.stderr)


if __name__ == "__main__":
    run()
