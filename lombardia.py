"""Fetch the environmental-impact procedures of Lombardia open for public observations from SILVIA, the Region's
public register (www.silvia.servizirl.it/silviaweb). Its "Agenda" pages call one JSON endpoint without login:
    GET silviaweb/getAllAgenda.html?statoF=100&idTipoEnte=<level>&idTipoProcedura=<kind>
The Agenda text says it lists the projects "in fase di consultazione e per i quali non si sono ancora conclusi i termini
per la presentazione di osservazioni da parte del pubblico". The field `dataScad` (dd/mm/yyyy) is that deadline: the
procedures seen on 2026-10-28 have dataScad = dataAvvio + 30 days, the term of art. 19 and art. 27-bis of d.lgs. 152/2006.
(The detail record's own `dataScadenzaOsservazioni` is often null, so it is not used.) `termine_osservazioni` = `dataScad`.
Empty filters return [] (tested), so one request per (level, kind) pair is needed. Level: 7 Region, 6 Province / Città
metropolitana, 5 Comune (11 = State is NOT asked: it is the MASE data fetch.py already carries; a "-MA" code is dropped
too). Kind: 3 VIA, 2 verifica di assoggettabilità. On 2026-10-28 only (7,3), (6,3), (6,2) returned data; the other three
are asked in case they fill up. robots.txt of the host: 404 (checked by hand 2026-10-28). If the host answers 403/429,
data/BLOCKED-LOMBARDIA is written and stops all further runs.

Requests per run: 6, PAUSE seconds apart; nothing is written unless all 6 succeed.

Rules:
- Included: items whose dataScad is today or later. Authority = the competent entity: "Regione Lombardia" (level 7),
  "Provincia di X" / "Città metropolitana di Milano" (level 6, X from the province suffix of the code, e.g. VER0713-BS),
  "Comune" (level 5; the register gives no name). The list is the Region's register but not only the Region's procedures.
- Title: the project description (first sentence, at most ~150 characters, "Richiedente:"/"Ditta" parts and any name of
  the proponent's people removed) + " - Comune (Provincia)" when the comune is in the text as "comune di X" or as the only
  "X (SIGLA)" in it, else " - Provincia di X" (or "Lombardia" for a regional item without a recognisable place).
- Proponent: the "Azienda"/"Ente" in the `proponenti` field; the people listed after it are never read. Dropped when it
  looks like a natural person (fetch.personlike / scrub) or when it ends in a "DI NAME SURNAME" without a company form.
- Link: the public summary page of the procedure on silvia.servizirl.it (route scheda-sintesi of the AngularJS app).
- "province": from the code suffix (Province items) or from "(SIGLA)" in the text; "regioni" is ["Lombardia"].

Usage: python3 lombardia.py
Writes data/lombardia.json (dict id -> record). Refuses to run while data/BLOCKED-LOMBARDIA exists.
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

from fetch import UA, scrub, iso, personlike, KEEP_CLOSED_DAYS
from sigle import SIGLA_PROVINCE, SIGLA_REGION

AGENDA = "https://www.silvia.servizirl.it/silviaweb/getAllAgenda.html?statoF=100&idTipoEnte={ente}&idTipoProcedura={kind}"
SCHEDA = "https://www.silvia.servizirl.it/silviaweb/#/scheda-sintesi?idTipoEnte={ente}&idTipoProcedura={kind}&idProgetto={pid}&idProvenienza=1"
HOME = "https://www.silvia.servizirl.it/silviaweb/"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "lombardia.json")
BLOCKED = os.path.join(HERE, "data", "BLOCKED-LOMBARDIA")
PAUSE = 4
COMBOS = [(7, 3), (7, 2), (6, 3), (6, 2), (5, 3), (5, 2)]
TYPES = {3: "Procedura di VIA", 2: "Verifica di Assoggettabilità a VIA"}
LOMBARD = sorted(s for s, r in SIGLA_REGION.items() if r == "Lombardia")
REGION = "Regione Lombardia"
COMUNE = "Comune"
PROVINCIA_ND = "Provincia (non indicata)"  # level 6 without a Lombard sigla in the code
ROME = ZoneInfo("Europe/Rome")


def provincia(sigla):
    return "Città metropolitana di Milano" if sigla == "MI" else "Provincia di " + SIGLA_PROVINCE[sigla]


AUTHORITIES = [REGION, COMUNE, PROVINCIA_ND] + [provincia(s) for s in LOMBARD]  # the values of "authority", for build.py's HOWTO
SMALL = {"di", "del", "della", "delle", "dei", "degli", "e", "ed", "al", "alla", "in", "per", "da", "sul", "sulla", "d"}
SUFFIX = [(r"\bS\.?R\.?L\.?(?=\W|$)", "S.r.l."), (r"\bS\.?P\.?A\.?(?=\W|$)", "S.p.A."), (r"\bS\.?N\.?C\.?(?=\W|$)", "S.n.c."),
          (r"\bS\.?A\.?S\.?(?=\W|$)", "S.a.s."), (r"\bS\.?S\.?(?=\W|$)", "S.s.")]
FORM = re.compile(r"\bS\.?R\.?L\.?|\bS\.?P\.?A\.?|\bGMBH\b|\bLTD\b", re.I)


def company(proponenti):
    """First "(Azienda: X)" or "(Ente: X)" of the field, cleaned; the people named after the colon are not read."""
    m = re.search(r"\((?:Azienda|Ente):\s*([^)]+?)\s*\)", proponenti or "")
    if not m:
        return None, None
    raw = re.split(r",?\s+O IN FORMA ABBREVIATA", m[1], flags=re.I)[0].strip(" ,.-")
    return raw, tidy(raw)


def tidy(raw):
    """Proponent name for display, or None when it may be a person (also "DITTA DI NAME SURNAME" forms)."""
    if not raw or personlike(raw):
        return None
    tail = re.search(r"\bDI\s+([A-ZÀ-Ý'][\wÀ-ÿ']+(?:\s+(?:E\s+)?[A-ZÀ-Ý'][\wÀ-ÿ']+){1,3})\s*(?:S\.?S\.?|S\.?N\.?C\.?|S\.?A\.?S\.?)?\s*$", raw, re.I)
    if tail and not FORM.search(raw) and not re.search(r"\b(?:COMUNE|PROVINCIA|REGIONE|CITTÀ METROPOLITANA|MINISTERO|CONSORZIO|ENTE)\b", raw, re.I):
        return None
    out = []
    for i, w in enumerate(raw.split()):
        if i and w.lower() in SMALL:
            out.append(w.lower())
        elif w.count(".") >= 2 and not re.fullmatch(r"S\.?[A-Z]\.?[A-Z]?\.?", w, re.I):
            out.append(w.upper())  # "TE.A.", "R.M.B."
        elif w.isupper() or w.islower():
            out.append("'".join(p[:1].upper() + p[1:].lower() for p in w.split("'")))
        else:
            out.append(w)
    s = " ".join(out)
    for pat, rep in SUFFIX:
        s = re.sub(pat, rep, s, flags=re.I)
    return s


def people(proponenti):
    """Names after the 'Azienda: X):' part, to be removed from titles (never stored)."""
    out = []
    for part in re.split(r"\(\s*(?:Azienda|Ente|Persona Fisica)[^)]*\)\s*:?", proponenti or ""):
        for n in re.split(r"[;]", part):
            n = " ".join(n.split()).strip(" :,")
            if len(n) > 4:
                out.append(n)
    return out


def loose(n):
    """Regex for a name as the register writes it: apostrophe/accent variants (SOCIETA' = SOCIETÀ) and final dot optional."""
    p = re.escape(n.strip(" .")).replace("\\ ", r"\s+")
    p = re.sub(r"([AEIOU])(?:'|’|\\')", lambda m: "(?:" + m[1] + "['’]|" + {"A": "À", "E": "È", "I": "Ì", "O": "Ò", "U": "Ù"}[m[1]] + ")", p)
    return p + r"\.?"


def sentence(descr, raw, names):
    s = " ".join((descr or "").replace("\r", " ").replace("\n", " ").split())
    s = re.sub(r"\[\s*ID[^\]]*\]\s*[-–]?\s*", "", s)  # "[ID 15049 - WEB-VIA ...]" prefix
    s = re.sub(r"\bRichiedente\s*:.*$", "", s, flags=re.I)
    s = re.sub(r"\b(?:Ditta|Soc\.)\s+[A-ZÀ-Ý0-9.&' -]{3,}?,\s*sede legale.*?(?=(?:\.\s)|$)", "la ditta", s)
    for n in [raw] + names:
        if n:
            s = re.sub(r'["“]?' + loose(n) + r'["”]?', "la ditta", s, flags=re.I)
    for n in names:  # whatever is left of a person's name: the full "Name Surname" / "Surname Name" only, on word boundaries
        ws = re.findall(r"[A-Za-zÀ-ÿ']+", n)
        if 2 <= len(ws) <= 4:
            for cand in (ws, ws[::-1]):
                s = re.sub(r"(?<![\wÀ-ÿ])" + r"\s+".join(re.escape(w) for w in cand) + r"(?![\wÀ-ÿ])", "", s, flags=re.I)
    s = re.sub(r"\b(?:la )+(la ditta),? di seguito denominata \"?Ditta\"?,?", r"\1", s, flags=re.I)
    s = re.sub(r"\b(?:la )+(?=la ditta)", "", s, flags=re.I)
    m = re.search(r"\brelativ\w+ (?:al|all['’]|ad un) ?(?:progetto|intervento)\s*(?:di\s+)?[:,]?\s*(.{12,})", s, re.I)
    if m:
        s = re.split(r"\s+(?:da realizzarsi|ubicat\w+|sit\w+)\b", m[1])[0]
    else:
        s = re.sub(r"^(?:Il progetto riguarda|Allo stato di fatto)\s+", "", s)
        cut = s.find(" ai sensi ")
        s = s[:cut] if cut >= 25 else s
    s = re.sub(r"\s+", " ", s).strip(" .,;-")
    first = re.split(r"(?<=[a-zà-ÿ0-9\)])\.\s+(?=[A-ZÀ-Ý])", s)[0]
    if first.count("(") > first.count(")"):  # cut inside an abbreviation in brackets: keep the whole text
        first = s
    if len(first) > 110:
        first = first[:110].rsplit(" ", 1)[0].rstrip(" ,;:-(")
        first += ")…" if first.count("(") > first.count(")") else "…"
    return first[:1].upper() + first[1:]


def comune_of(descr):
    d = " ".join((descr or "").split())
    m = re.search(r"\b[Cc]omun[ei](?:/i)?\s+(?:di\s+)?([A-ZÀ-Ý][\wÀ-ÿ'’]*(?:\s+(?:(?:di|del|della|dei|d[’'])\s*)?[A-ZÀ-Ý][\wÀ-ÿ'’]*){0,3}?)(?=\s*\(|[,.;]|\s+(?:in|ai|per|loc|località|via|ed?)\b|$)", d)
    sig = {(n.strip(), s) for n, s in re.findall(r"([A-ZÀ-Ý][\wÀ-ÿ'’. ]{2,30}?)\s*\(\s*([A-Z]{2})\s*\)", d) if s in LOMBARD}
    if m:
        sg = re.search(re.escape(m[1]) + r"\s*\(\s*([A-Z]{2})\s*\)", d)
        return m[1].strip(), sg[1] if sg and sg[1] in LOMBARD else None
    if len(sig) == 1:
        n, s = next(iter(sig))
        n = re.sub(r"^.*?\b(?:a|in|di|nel|presso|sito in|sita in)\s+(?=[A-ZÀ-Ý])", "", n)
        return n, s
    return None, None


def record(x, old, today, now):
    ente, kind = int(x["idTipoEnte"]), int(x["idTipoProcedura"])
    code = x["codProcedura"]
    suffix = code.rsplit("-", 1)[-1] if "-" in code else ""
    com, csig = comune_of(x["descrProgetto"])
    psig = suffix if suffix in LOMBARD else csig
    raw, prop = company(x["proponenti"])
    what = sentence(x["descrProgetto"], raw, people(x["proponenti"]))
    if len(what) < 60 and prop and prop not in what:
        what += " - " + prop
    if ente == 7:
        auth = REGION
    elif ente == 6:
        auth = provincia(suffix) if suffix in LOMBARD else PROVINCIA_ND
    else:
        auth = COMUNE
    if com:
        where = com + (f" ({psig})" if psig else "")
    else:
        where = provincia(psig) if psig else "Lombardia"
    avvio = iso(x["dataAvvio"])
    rec = {"id": f"lombardia-{x['idProgetto']}", "authority": auth, "codice": code,
           "url": SCHEDA.format(ente=ente, kind=kind, pid=x["idProgetto"]), "procedure_type": TYPES[kind], "tipologia": None,
           "title": f"{what} - {where}", "proponente": prop, "regioni": ["Lombardia"],
           "province": [SIGLA_PROVINCE[psig]] if psig else [], "comuni": [com] if com else [], "avvio": avvio,
           "termine_osservazioni": iso(x["dataScad"]), "termini": [iso(x["dataScad"])], "stato": None,
           "first_seen": old.get("first_seen", today), "last_seen": today, "closed_at": None,
           "updated_at": now if old.get("termine_osservazioni") != iso(x["dataScad"]) else old.get("updated_at", now)}
    return scrub(rec)


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "it"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def run():
    if os.path.exists(BLOCKED):
        sys.exit(f"data/BLOCKED-LOMBARDIA exists, not running: {open(BLOCKED).read().strip()}")
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = dt.datetime.now(ROME).date().isoformat()
    procs = json.load(open(DATA, encoding="utf-8")) if os.path.exists(DATA) else {}
    rows = []
    for i, (ente, kind) in enumerate(COMBOS):
        url = AGENDA.format(ente=ente, kind=kind)
        try:
            got = get(url)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                open(BLOCKED, "w").write(f"{now} HTTP {e.code} on {url}\n")
            sys.exit(f"lombardia: HTTP {e.code} on {url}")
        except (urllib.error.URLError, ValueError) as e:
            sys.exit(f"lombardia: {e} on {url}: nothing written")
        if not isinstance(got, list):
            sys.exit(f"lombardia: unexpected answer on {url}: nothing written")
        rows += [dict(x, idTipoEnte=str(ente), idTipoProcedura=str(kind)) for x in got]
        if i < len(COMBOS) - 1:
            time.sleep(PAUSE)
    stored_open = {k for k, v in procs.items() if not v.get("closed_at")}
    if not rows and stored_open:
        sys.exit("lombardia: no rows at all but open records are stored: endpoint changed? nothing written")
    seen, national, past, bad, dup, keys = {}, 0, 0, 0, 0, set()
    for x in rows:
        if x["codProcedura"].endswith("-MA"):
            national += 1
            continue
        k = (x["idTipoEnte"], x["idTipoProcedura"], x["idProgetto"])
        if k in keys or f"lombardia-{x['idProgetto']}" in seen:
            dup += 1
            print(f"lombardia: duplicate id {x['idProgetto']} (ente/kind {k[0]}/{k[1]})", file=sys.stderr)
        keys.add(k)
        d = iso(x.get("dataScad"))
        if not d:
            bad += 1
            if x.get("dataScad") and x["dataScad"].strip():
                print(f"lombardia: unparsable dataScad {x['dataScad']!r} on {x['idProgetto']}", file=sys.stderr)
        elif d < today:
            past += 1
        else:
            rec = record(x, procs.get(f"lombardia-{x['idProgetto']}", {}), today, now)
            seen[rec["id"]] = rec
    # a row without a readable deadline might be open: never read that as "closed"
    if bad:
        sys.exit(f"lombardia: {bad} rows without a readable dataScad (field renamed?): nothing written")
    still_open = {k for k in stored_open if (procs[k].get("termine_osservazioni") or "") >= today}
    if still_open and len(still_open & set(seen)) < len(still_open) / 2:
        sys.exit(f"lombardia: only {len(still_open & set(seen))} of {len(still_open)} stored still-open records seen: "
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
    print(f"lombardia: {len(rows)} items, {national} national (dropped), {past} past deadline (dropped), "
          f"{len(seen)} open, stored {len(procs)}", file=sys.stderr)


if __name__ == "__main__":
    run()
