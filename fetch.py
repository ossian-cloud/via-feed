"""Fetch environmental assessment procedures open for public observations from the
MASE portal Valutazioni e Autorizzazioni Ambientali (va.mite.gov.it), keeping slim,
person-free records.

Usage: python3 fetch.py [--cache DIR] [--max-detail N]
Reads the home listing and page 1 of "Avvisi al pubblico", then fetches detail pages
only for new procedures or procedures whose deadline changed.
Writes data/procedures.json (dict id -> record). Refuses to run while data/BLOCKED exists.
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

BASE = "https://va.mite.gov.it"
UA = "OssianBot/0.1 (+https://ossian.cloud; ossian@ossian.cloud)"
PAUSE = 3  # seconds between requests
MAX_DETAIL = 40
KEEP_CLOSED_DAYS = 60
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "procedures.json")
BLOCKED = os.path.join(HERE, "data", "BLOCKED")
PORTAL = os.path.join(HERE, "data", "portal.json")
BLOCKLIST = os.path.join(HERE, "blocklist.txt")  # ids removed on request, one per line
VIA = "Valutazione Impatto Ambientale"

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+|\bmailto:", re.I)
PHONE = re.compile(r"\btel\b|(?<![\d,.])(?:\+?39[\s.-]?)?(?:0\d{1,3}[\s.-]?\d{5,8}|3\d{2}[\s.-]?\d{6,7})(?![\d,])", re.I)
FISCAL = re.compile(r"\b[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]\b")
COMPANY = re.compile(r"\b(?:s\.?r\.?l|s\.?p\.?a|s\.?a\.?s|s\.?n\.?c|s\.?c\.?a?r?\.?l|s\.?a|gmbh|ltd|b\.?v|llc|inc|"
                     r"societ\w*|autorit\w*|comune|regione|provincia|ministero|consorzio|ente|agenzia|azienda|"
                     r"fondazione|cooperativa|coop|gruppo|energia|energy|power|solar|wind|green|renewable|"
                     r"italia|italiana|group|holding|porto|porti|anas|rfi|enel|eni|terna|snam|a2a|iren|"
                     r"unione|università|consortium|company|spa|srl|srls|park|impianti|costruzioni|"
                     r"comunit\w*|consorz\w*|direzion\w*|rinnovabil\w*)\b", re.I)
NAMELIKE = re.compile(r"^(?:ditta |impresa individuale )?[A-ZÀ-Ý][a-zà-ÿ']+(?: (?:de|di|del|della|dal|d'|la|lo)?\s?[A-ZÀ-Ý][a-zà-ÿ']+){1,3}$")
TITLES = re.compile(r"^(?:(?:ing|dott|dott\.ssa|arch|geom|avv|sig|sig\.ra|prof)\.?\s+)+", re.I)


FIRST_NAMES = set("""alberto alessandra alessandro alfredo angelo antonella antonio armando arturo attilio augusto beatrice benedetto
bruno carla carlo carmela carmelo caterina cesare claudia claudio concetta cosimo cristina daniela daniele danilo davide
domenico donato edoardo elena elisa emanuele enrico enzo erica ernesto eugenio fabio fabrizio federica federico felice
ferdinando filippo francesca francesco franco gabriele gaetano giacomo gianluca gianni giancarlo gianpaolo giorgio
giovanna giovanni giulia giuliano giulio giuseppe giuseppina graziano guido ignazio ilaria irene italo laura leonardo
lorenzo luca luciano lucia luigi luisa manuel manuela marcello marco maria mariano marina mario martina massimo matteo
mauro michele michela monica nicola nicolo nunzio orazio oreste paola paolo pasquale patrizia patrizio pierluigi piero
pietro raffaele renato riccardo roberta roberto rocco rosa rosario rosanna salvatore samuele sandro sara saverio sergio
silvia silvio simona simone stefania stefano tommaso ugo umberto valentina valerio vincenzo vittorio vito walter""".split())
# a first name preceded by one of these is a place or saint, not a person ("Azienda Agricola San Martino")
SAINT = {"san", "santa", "santo", "sant", "monte", "villa", "borgo", "podere", "masseria", "tenuta", "fondo", "casa", "cascina"}
SOLE_PREFIX = re.compile(r"^(?:azienda agricola|azienda|az\.? ?agr\.?(?:icola)?|societ[àa]['’]? agricola|soc\.? ?agr\.?(?:icola)?|ditta|impresa)\s+(.+)$", re.I)
SOLE_SUFFIX = re.compile(r"^(.+?)\s*(?:&|\be)\s*C\.?$|^(.+?)\s+S\.?\s?S\.?$", re.I)


def _sole_trader(q):
    """Sole-trader forms that carry a company keyword: 'Azienda Agricola Mario Rossi', 'AZ. AGR. ROSSI MARIO',
    'Società Agricola Rossi Mario', 'Rossi Mario & C.', 'Mario Rossi S.s.': 2-3 plain words, one of them a first name."""
    m = SOLE_PREFIX.match(q)
    body = m[1] if m else None
    if body is None:
        m = SOLE_SUFFIX.match(q)
        body = (m[1] or m[2]) if m else None
    if body is None:
        return False
    body = re.sub(r"\s*(?:&|\be)\s*C\.?$|\s+S\.?\s?S\.?$", "", body.strip(), flags=re.I)
    ws = body.split()
    if not 2 <= len(ws) <= 3 or not all(re.fullmatch(r"[A-Za-zÀ-ÿ']+", w) for w in ws):
        return False
    low = [w.lower() for w in ws]
    if any(w in SAINT for w in low):
        return False
    return any(w in FIRST_NAMES for w in low)


def personlike(p):
    """Words only, in any case, 2 to 12 of them, no company or public-body keyword: likely one or more people
    ('GRIGOLI GIANCARLO', 'ing. Mario Rossi', 'METALFER di Coppola Antonio'). Some small firms get dropped too: safe side.
    Sole-trader forms with a keyword ('Azienda Agricola Mario Rossi', 'Rossi Mario & C.', 'Mario Rossi S.s.') are caught
    by _sole_trader."""
    q = TITLES.sub("", p.strip())
    if _sole_trader(q):
        return True
    ws = re.findall(r"[^\s,]+", q)
    return not COMPANY.search(q) and 2 <= len(ws) <= 12 and all(re.fullmatch(r"[A-Za-zÀ-ÿ']+", w) for w in ws)


class Blocked(Exception):
    pass


_last = 0.0
n_req = 0


def get(url):
    global _last, n_req
    wait = PAUSE - (time.time() - _last)
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "it"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            body = r.read().decode("utf-8-sig", "replace")
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            raise Blocked(f"HTTP {e.code} on {url}")
        raise
    finally:
        _last = time.time()
        n_req += 1
    if re.search(r"captcha|Access Denied", body, re.I):
        raise Blocked(f"captcha or block page on {url}")
    return body


def clean(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def iso(s):
    m = re.search(r"(\d\d)/(\d\d)/(\d{4})", s or "")
    return f"{m[3]}-{m[2]}-{m[1]}" if m else None


def parse_home(h):
    out = []
    for blk in re.split(r'<div class="size_box">', h)[1:]:
        m = re.search(r"Oggetti/Info/(\d+)", blk)
        dl = re.search(r'class="scadenza">.*?<strong>([^<]+)</strong>', blk, re.S)
        if not (m and dl):
            continue
        out.append({"id": m[1], "procedure_type": clean(re.search(r"<h3[^>]*>(.*?)</h3>", blk, re.S)[1]),
                    "tipologia": (re.search(r'alt="([^"]*)"', blk) or [0, ""])[1] or None,
                    "title": clean(re.search(r'<a href="[^"]*Info[^>]*>(.*?)</a>', blk, re.S)[1]),
                    "termine": iso(dl[1])})
    return out


def parse_avvisi(h):
    out = []
    for row in re.split(r"<tr>", h)[1:]:
        tds = re.findall(r"<td>(.*?)</td>", row, re.S)
        m = re.search(r"Oggetti/Info/(\d+)", row)
        if len(tds) < 3 or not m:
            continue
        out.append({"id": m[1], "procedure_type": VIA, "tipologia": None, "title": clean(tds[0]),
                    "avvio": iso(tds[1]), "termine": iso(tds[2])})
    return out


START = (r"Data avvio (?:nuova )?consultazione pubblica|Data comunicazione avvio nuova consultazione pubblica"
         r"|Data (?:[IVX]+ )?ripubblicazione[^:]{0,60}"
         r"|Data pubblicazione avviso sul sito web dell.autorit\w* competente")
END = r"Termine (?:per la )?presentazione (?:delle )?osservazioni[^:]*"


def last_date(t, label, until=None):
    # multi-round procedures repeat these labels; the newest round is the open one.
    # AIA pages hold several procedures: a start after the open one's deadline belongs to another
    ds = [iso(x) for x in re.findall(r"(?:" + label + r")\s*:\s*(\d\d/\d\d/\d{4})", t, re.I)]
    ds = [x for x in ds if not until or x <= until]
    return max(ds) if ds else None


def rounds(t):
    """(start, deadline) of each consultation round in page order: a deadline pairs with the start just before it."""
    out, start = [], None
    for m in re.finditer(r"(" + START + "|" + END + r")\s*:\s*(\d\d/\d\d/\d{4})", t, re.I):
        if re.match(END, m[1], re.I):
            out.append((start, iso(m[2])))
            start = None
        else:
            start = iso(m[2])
    return out


def procedure_block(t, termini):
    """A project page can hold several procedures (a VIA, a later screening, compliance checks), each in a block
    starting at 'Codice procedura (ID_VIP/ID_MATTM)'. The open one is the block with a listed deadline.
    Returns (id_vip, block text), or (None, None) when no block matches (AIA pages carry no deadline)."""
    for b in re.split(r"Codice procedura \(ID_VIP/ID_MATTM\)\s*", t)[1:]:
        if termini & {d for _, d in rounds(b)}:
            m = re.match(r"(\d+(?:/\d+)?)", b)
            return (m[1] if m else None), b
    return None, None


def parse_detail(h, termini=()):
    """termini: the deadlines the listings show for this page (two when two rounds are open at once)."""
    t = clean(re.sub(r"(?s)<(script|style).*?</\1>", "", h))
    termini = set(termini)
    id_vip, blk = procedure_block(t, termini)
    src = blk or t

    def f(label, stop, text=t):
        m = re.search(label + r"\s*:?\s*(.*?)\s*(?:" + stop + ")", text)
        return m[1].strip() or None if m else None

    def lst(s):
        return [x.strip() for x in (s or "").split(",") if x.strip() and not x.startswith("Nessun")]

    stato = f("Stato procedura:", r"Aggiornamento|Riesame|Rinnovo|Valutazione|Verifica di|Autorizzazione|Definizione contenuti|Comunic-Azione|Dettagli|Data |Note|$", src)
    out = {"title": f("Progetto :", "Descrizione :|Proponente|Tipologia") or f("Installazione :", "Localizzazione|Tipologia"),
           "proponente": f("Proponente :", "Tipologia di opera|Scadenza|Territori") or f("Gestore :", "Codice Fiscale|Stato installazione"),
           "tipologia": f("Tipologia di opera :", "Scadenza|Territori") or f("Tipologia installazione :", "Categoria|Gestore"),
           "regioni": lst(f("Regioni:", "Province")), "province": lst(f("Province:", "Comuni")),
           "comuni": lst(f("Comuni:", "Aree marine")),
           "avvio": last_date(src, START, min(termini) if termini else None),
           "termine": last_date(t, END),
           "stato": re.split(r"\s+\d{4,}|\s+\d\d/\d\d/\d{4}", stato)[0][:80] if stato else None}
    if blk:
        by_end = {}  # deadline -> start; a repeated deadline is one round
        for a, d in rounds(blk):
            if d in termini:
                by_end[d] = by_end.get(d) or a
        open_rounds = sorted(by_end.items())
        out["avvio"] = open_rounds[0][1] or out["avvio"]
        out["id_vip"] = id_vip
        out["oggetto"] = f(r"^[\d/]+\s+Oggetto:", r"Procedura integrata|Data (?:prot|presentazione|richiesta|ricezione|avvio|comunicazione"
                          r"|pubblicazione|(?:[IVX]+ )?ripubblicazione)\b|Note:|Responsabile|Stato procedura", blk)
        out["note"] = f("Note:", r"Responsabile del procedimento|Stato procedura", blk)
        if len(open_rounds) > 1:
            out["rounds"] = [[a, d] for d, a in open_rounds]
    for k in ("title", "oggetto"):  # since 29 Oct some pages prefix "[ID_13503-VIAVIAF00000369]": scrub() took the code for a phone
        if out.get(k):
            out[k] = re.sub(r"^\[ID_[^\]]*\]\s*", "", out[k]) or None
    return out


def personal(s):
    return bool(EMAIL.search(s) or PHONE.search(s) or FISCAL.search(s) or re.search(r"responsabil|\bRUP\b", s, re.I))


def scrub(rec):
    """Drop every field that carries contact data or may name a natural person."""
    for k, v in list(rec.items()):
        if isinstance(v, list):
            rec[k] = [x for x in v if not (isinstance(x, str) and personal(x))]
        elif isinstance(v, str) and personal(v):
            rec[k] = None
    if rec.get("proponente"):  # VAS pages run the next fields into the proponente
        rec["proponente"] = re.split(r"\s+Settore di pianificazione\s*:", rec["proponente"])[0].strip()
    p = rec.get("proponente")
    if p and (NAMELIKE.match(p) and not COMPANY.search(p) or personlike(p)):
        rec["proponente"] = None
    return rec


def load_json(path, default):
    return json.load(open(path, encoding="utf-8")) if os.path.exists(path) else default


def save(procs):
    os.makedirs(os.path.dirname(DATA), exist_ok=True)
    with open(DATA + ".tmp", "w", encoding="utf-8") as f:
        json.dump(procs, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(DATA + ".tmp", DATA)


def block(reason):
    os.makedirs(os.path.dirname(BLOCKED), exist_ok=True)
    open(BLOCKED, "w").write(f"{dt.datetime.now(dt.timezone.utc).isoformat()} {reason}\n")


def fetch_detail(pid, cache):
    path = os.path.join(cache, f"d{pid}.html") if cache else None
    if path and os.path.exists(path):
        return open(path, encoding="utf-8-sig").read()
    h = get(f"{BASE}/it-IT/Oggetti/Info/{pid}")
    if path:
        open(path, "w", encoding="utf-8").write(h)
    return h


def merge_listing(home, avvisi, today):
    """Open procedures seen in the listings: id -> listing record (home wins), with "termini", every listed
    deadline, and "termine", the earliest: the home lists a page twice when two rounds are open at once."""
    seen, termini = {}, {}
    for x in [x for x in avvisi if x["termine"] and x["termine"] >= today] + home:
        seen[x["id"]] = dict(x)
        if x["termine"]:
            termini.setdefault(x["id"], set()).add(x["termine"])
    for pid, x in seen.items():
        x["termini"] = sorted(termini.get(pid, []))
        x["termine"] = x["termini"][0] if x["termini"] else None
    return seen


def run(cache, max_detail):
    if os.path.exists(BLOCKED):
        sys.exit(f"data/BLOCKED exists, not running: {open(BLOCKED).read().strip()}")
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = now[:10]
    procs = load_json(DATA, {})
    blocked_ids = set(open(BLOCKLIST).read().split()) if os.path.exists(BLOCKLIST) else set()
    try:
        home_html = get(BASE + "/it-IT")
        home = parse_home(home_html)
        avvisi = parse_avvisi(get(BASE + "/it-IT/Procedure/AvvisiAlPubblico"))
    except Blocked as e:
        block(str(e))
        sys.exit(f"stopped: {e}")
    if not home:
        sys.exit("home listing parsed to zero procedures: layout changed? nothing written")
    seen = merge_listing(home, avvisi, today)
    # the portal's banner about online submissions being disabled (since at least 9 Oct 2026); build.py says so
    with open(PORTAL, "w", encoding="utf-8") as f:
        json.dump({"checked": today, "online_disabled": bool(re.search(r"temporaneamente\s+(?:stato\s+)?disabilitat", home_html))}, f)
    for pid in blocked_ids:
        seen.pop(pid, None)
        procs.pop(pid, None)
    todo = [pid for pid, x in seen.items() if pid not in procs or not procs[pid].get("title")
            or procs[pid].get("termine_osservazioni") != x["termine"]
            or procs[pid].get("termini") != x["termini"]]
    print(f"home {len(home)}, avvisi {len(avvisi)}, open {len(seen)}, detail to fetch {len(todo)} (cap {max_detail})", file=sys.stderr)
    try:
        for pid in todo[:max_detail]:
            x = seen[pid]
            d = {k: v for k, v in parse_detail(fetch_detail(pid, cache), x["termini"]).items() if v}
            old = procs.get(pid, {})
            rec = {"id": pid, "url": f"{BASE}/it-IT/Oggetti/Info/{pid}", "procedure_type": x["procedure_type"],
                   "tipologia": d.get("tipologia") or x["tipologia"], "title": d.get("title") or x["title"],
                   "proponente": d.get("proponente"), "regioni": d.get("regioni", []), "province": d.get("province", []),
                   "comuni": d.get("comuni", []), "avvio": d.get("avvio") or x.get("avvio"),
                   "termine_osservazioni": x["termine"] or d.get("termine"), "termini": x["termini"], "stato": d.get("stato"),
                   "id_vip": d.get("id_vip"), "oggetto": d.get("oggetto"), "note": d.get("note"), "rounds": d.get("rounds"),
                   "first_seen": old.get("first_seen", today), "last_seen": today, "closed_at": None,
                   "updated_at": now if old.get("termine_osservazioni") != (x["termine"] or d.get("termine")) else old.get("updated_at", now)}
            procs[pid] = scrub(rec)
    except Blocked as e:
        block(str(e))
        print(f"stopped: {e}", file=sys.stderr)
    for pid, r in procs.items():
        if pid in seen:
            r["last_seen"], r["closed_at"] = today, None
        elif not r.get("closed_at"):
            r["closed_at"] = today
    limit = (dt.date.fromisoformat(today) - dt.timedelta(days=KEEP_CLOSED_DAYS)).isoformat()
    procs = {k: v for k, v in procs.items() if not v.get("closed_at") or v["closed_at"] >= limit}
    save(procs)
    print(f"stored {len(procs)} procedures, {n_req} requests", file=sys.stderr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", help="directory of cached detail pages (read and written)")
    ap.add_argument("--max-detail", type=int, default=MAX_DETAIL)
    a = ap.parse_args()
    run(a.cache, min(a.max_detail, MAX_DETAIL))
