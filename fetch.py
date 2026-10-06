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
BLOCKLIST = os.path.join(HERE, "blocklist.txt")  # ids removed on request, one per line
VIA = "Valutazione Impatto Ambientale"

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+|\bmailto:", re.I)
PHONE = re.compile(r"\btel\b|(?<![\d,.])(?:\+?39[\s.-]?)?(?:0\d{1,3}[\s.-]?\d{5,8}|3\d{2}[\s.-]?\d{6,7})(?![\d,])", re.I)
FISCAL = re.compile(r"\b[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]\b")
COMPANY = re.compile(r"\b(?:s\.?r\.?l|s\.?p\.?a|s\.?a\.?s|s\.?n\.?c|s\.?c\.?a?r?\.?l|s\.?a|gmbh|ltd|b\.?v|llc|inc|"
                     r"societ|autorit|comune|regione|provincia|ministero|consorzio|ente|agenzia|azienda|"
                     r"fondazione|cooperativa|coop|gruppo|energia|energy|power|solar|wind|green|renewable|"
                     r"italia|italiana|group|holding|porto|porti|anas|rfi|enel|eni|terna|snam|a2a|iren|"
                     r"unione|università|consortium|company|spa|srl|park|impianti|costruzioni)\b", re.I)
NAMELIKE = re.compile(r"^(?:ditta |impresa individuale )?[A-ZÀ-Ý][a-zà-ÿ']+(?: (?:de|di|del|della|dal|d'|la|lo)?\s?[A-ZÀ-Ý][a-zà-ÿ']+){1,3}$")


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


def last_date(t, label):
    # multi-round procedures repeat these labels; the newest round is the open one
    ds = [iso(x) for x in re.findall(r"(?:" + label + r")\s*:\s*(\d\d/\d\d/\d{4})", t, re.I)]
    return max(ds) if ds else None


def parse_detail(h):
    t = clean(re.sub(r"(?s)<(script|style).*?</\1>", "", h))

    def f(label, stop):
        m = re.search(label + r"\s*:?\s*(.*?)\s*(?:" + stop + ")", t)
        return m[1].strip() or None if m else None

    def lst(s):
        return [x.strip() for x in (s or "").split(",") if x.strip() and not x.startswith("Nessun")]

    stato = f("Stato procedura:", r"Aggiornamento|Riesame|Rinnovo|Valutazione|Verifica di|Autorizzazione|Comunic-Azione|Dettagli|Data |Note|$")
    return {"title": f("Progetto :", "Proponente|Tipologia") or f("Installazione :", "Localizzazione|Tipologia"),
            "proponente": f("Proponente :", "Tipologia di opera|Scadenza|Territori") or f("Gestore :", "Codice Fiscale|Stato installazione"),
            "tipologia": f("Tipologia di opera :", "Scadenza|Territori") or f("Tipologia installazione :", "Categoria|Gestore"),
            "regioni": lst(f("Regioni:", "Province")), "province": lst(f("Province:", "Comuni")),
            "comuni": lst(f("Comuni:", "Aree marine")),
            "avvio": last_date(t, r"Data avvio (?:nuova )?consultazione pubblica|Data comunicazione avvio nuova consultazione pubblica"),
            "termine": last_date(t, r"Termine (?:per la )?presentazione (?:delle )?osservazioni[^:]*"),
            "stato": stato[:80] if stato else None}


def personal(s):
    return bool(EMAIL.search(s) or PHONE.search(s) or FISCAL.search(s) or re.search(r"responsabil|\bRUP\b", s, re.I))


def scrub(rec):
    """Drop every field that carries contact data or may name a natural person."""
    for k, v in list(rec.items()):
        if isinstance(v, list):
            rec[k] = [x for x in v if not personal(x)]
        elif isinstance(v, str) and personal(v):
            rec[k] = None
    if rec.get("proponente"):  # VAS pages run the next fields into the proponente
        rec["proponente"] = re.split(r"\s+Settore di pianificazione\s*:", rec["proponente"])[0].strip()
    p = rec.get("proponente")
    if p and NAMELIKE.match(p) and not COMPANY.search(p):
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
    """Open procedures seen in the listings: id -> listing record (home wins)."""
    seen = {x["id"]: x for x in avvisi if x["termine"] and x["termine"] >= today}
    seen.update({x["id"]: x for x in home})
    return seen


def run(cache, max_detail):
    if os.path.exists(BLOCKED):
        sys.exit(f"data/BLOCKED exists, not running: {open(BLOCKED).read().strip()}")
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = now[:10]
    procs = load_json(DATA, {})
    blocked_ids = set(open(BLOCKLIST).read().split()) if os.path.exists(BLOCKLIST) else set()
    try:
        home = parse_home(get(BASE + "/it-IT"))
        avvisi = parse_avvisi(get(BASE + "/it-IT/Procedure/AvvisiAlPubblico"))
    except Blocked as e:
        block(str(e))
        sys.exit(f"stopped: {e}")
    if not home:
        sys.exit("home listing parsed to zero procedures: layout changed? nothing written")
    seen = merge_listing(home, avvisi, today)
    for pid in blocked_ids:
        seen.pop(pid, None)
        procs.pop(pid, None)
    todo = [pid for pid, x in seen.items() if pid not in procs or procs[pid].get("termine_osservazioni") != x["termine"]]
    print(f"home {len(home)}, avvisi {len(avvisi)}, open {len(seen)}, detail to fetch {len(todo)} (cap {max_detail})", file=sys.stderr)
    try:
        for pid in todo[:max_detail]:
            x = seen[pid]
            d = {k: v for k, v in parse_detail(fetch_detail(pid, cache)).items() if v}
            old = procs.get(pid, {})
            rec = {"id": pid, "url": f"{BASE}/it-IT/Oggetti/Info/{pid}", "procedure_type": x["procedure_type"],
                   "tipologia": d.get("tipologia") or x["tipologia"], "title": d.get("title") or x["title"],
                   "proponente": d.get("proponente"), "regioni": d.get("regioni", []), "province": d.get("province", []),
                   "comuni": d.get("comuni", []), "avvio": d.get("avvio") or x.get("avvio"),
                   "termine_osservazioni": x["termine"] or d.get("termine"), "stato": d.get("stato"),
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
