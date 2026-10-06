"""Build Atom feeds, .ics calendars and the index page from data/procedures.json.

Usage: python3 build.py OUTDIR   (e.g. /tmp/via-out)
"""
import datetime as dt
import html
import json
import os
import re
import sys
import zoneinfo

from atom import write_feed
from ics import write_calendar

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "procedures.json")
BASE = "https://ossian.cloud/via"
SOURCE = ("Fonte: Ministero dell'Ambiente e della Sicurezza Energetica, "
          "portale Valutazioni Ambientali (va.mite.gov.it)")
DISCLAIMER = ("Estratto non ufficiale: fa fede il portale del Ministero; "
              "verificare termini e modalità sul sito ufficiale.")
RIGHTS = f"{SOURCE}. {DISCLAIMER} Feed generato da Ossian, un agente AI, senza alcun legame con il Ministero."
ROME = zoneinfo.ZoneInfo("Europe/Rome")
MAX_ENTRIES = 200

REGIONS = ["Abruzzo", "Basilicata", "Calabria", "Campania", "Emilia-Romagna", "Friuli-Venezia Giulia",
           "Lazio", "Liguria", "Lombardia", "Marche", "Molise", "Piemonte", "Puglia", "Sardegna",
           "Sicilia", "Toscana", "Trentino-Alto Adige", "Umbria", "Valle d'Aosta", "Veneto"]
# group key -> (label, regex on the tipologia)
GROUPS = {
    "eolico": ("Eolico", r"eolic"),
    "fotovoltaico-agrivoltaico": ("Fotovoltaico e agrivoltaico", r"voltaic"),
    "infrastrutture": ("Infrastrutture (strade, porti, aeroporti, opere idrauliche)",
                       r"stradal|portual|aeropor|ferrovi|metropol|idraulic|autostrad|interport"),
    "industria-energia": ("Industria ed energia (centrali, raffinerie, idrocarburi, elettrodotti)",
                          r"central|raffiner|chimic|idrocarbur|rigassific|elettrodott|minerari|gasdott|stoccagg"),
    "altro": ("Altro (VAS e altre opere)", r"(?!)"),
}
TYPES = [("Verifica di Assoggettabilit", "Verifica VIA"), ("Autorizzazione Integrata", "AIA"),
         ("Valutazione Impatto", "VIA"), ("Valutazione Ambientale Strategica", "VAS")]


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower().replace("'", "")).strip("-")


def day(iso):
    return iso[8:10] + "/" + iso[5:7] + "/" + iso[0:4] if iso else None


def short(s, n):
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def type_label(p):
    t = p["procedure_type"]
    return next((lab for key, lab in TYPES if t.startswith(key)), t)


def group_of(p):
    t = p.get("tipologia") or ""
    for key, (_, rx) in GROUPS.items():
        if re.search(rx, t, re.I):
            return key
    return "altro"


def regions_of(p):
    by_slug = {slug(r): r for r in REGIONS}
    return sorted({by_slug.get(slug(r), r) for r in p.get("regioni", [])})


def place(p):
    # the portal sometimes lists a region or province name among the comuni
    not_comuni = {slug(x) for x in REGIONS + p.get("regioni", []) + p.get("province", [])}
    comuni = [c for c in p.get("comuni", []) if slug(c) not in not_comuni]
    bits = [", ".join(comuni), ", ".join(p.get("province", [])) and "prov. " + ", ".join(p["province"]),
            ", ".join(regions_of(p))]
    return " · ".join(b for b in bits if b) or None


def entry(p):
    e = html.escape
    rows = [("Procedura", p["procedure_type"]), ("Proponente", p.get("proponente")),
            ("Tipologia", p.get("tipologia")), ("Luogo", place(p)),
            ("Avvio consultazione", day(p.get("avvio"))),
            ("Termine osservazioni", day(p["termine_osservazioni"])), ("Stato", p.get("stato"))]
    parts = [f"<p>{e(p['title'])}</p>", "<ul>"] + [f"<li><b>{k}:</b> {e(v)}</li>" for k, v in rows if v] + ["</ul>"]
    parts.append(f'<p><a href="{e(p["url"])}">Scheda ufficiale sul portale del Ministero</a></p>')
    parts.append(f"<p><small>{e(SOURCE)}. {e(DISCLAIMER)}</small></p>")
    upd = p["updated_at"]
    avvio = p.get("avvio")
    pub = avvio + "T00:00:00Z" if avvio and avvio <= upd[:10] else p["first_seen"] + "T00:00:00Z"
    cats = [("tipo:" + slug(type_label(p)), type_label(p)), ("gruppo:" + group_of(p), p.get("tipologia") or "Altro")]
    cats += [("regione:" + slug(r), r) for r in regions_of(p)]
    return {"id": f"tag:ossian.cloud,2026:via/{p['id']}",
            "title": f"[{type_label(p)}] {short(p['title'], 200)} (osservazioni entro il {day(p['termine_osservazioni'])})",
            "link": p["url"], "published": min(pub, upd), "updated": upd,
            "summary_html": "".join(parts), "categories": cats}


def event(p):
    lines = [p["title"], f"Proponente: {p['proponente']}" if p.get("proponente") else None,
             f"Procedura: {p['procedure_type']}", f"Tipologia: {p['tipologia']}" if p.get("tipologia") else None,
             f"Luogo: {place(p)}" if place(p) else None, f"Scheda ufficiale: {p['url']}", SOURCE + ".", DISCLAIMER]
    return {"uid": f"via-{p['id']}@ossian.cloud", "date": p["termine_osservazioni"], "stamp": p["updated_at"],
            "summary": "Osservazioni: " + short(p["title"], 150), "description": "\n".join(l for l in lines if l),
            "url": p["url"]}


def open_procedures(procs, today):
    out = [p for p in procs.values() if not p.get("closed_at") and p.get("termine_osservazioni")
           and p["termine_osservazioni"] >= today]
    return sorted(out, key=lambda p: (p["termine_osservazioni"], p["id"]))


def write_all(outdir, items, latest):
    os.makedirs(os.path.join(outdir, "feed"), exist_ok=True)
    os.makedirs(os.path.join(outdir, "calendario"), exist_ok=True)
    sets = {"tutte": ("Tutta Italia", items)}
    for r in REGIONS:
        sets[slug(r)] = (r, [p for p in items if r in regions_of(p)])
    for key, (label, _) in GROUPS.items():
        sets[key] = (label, [p for p in items if group_of(p) == key])
    for key, (label, sel) in sets.items():
        by_upd = sorted(sel, key=lambda p: p["updated_at"], reverse=True)[:MAX_ENTRIES]
        write_feed(os.path.join(outdir, "feed", key + ".xml"), feed_id=f"tag:ossian.cloud,2026:via/feed/{key}",
                   title=f"Consultazioni ambientali (VIA, VAS, AIA) · {label}",
                   subtitle="Procedure aperte alle osservazioni del pubblico sul portale Valutazioni Ambientali del Ministero",
                   self_url=f"{BASE}/feed/{key}.xml", alt_url=f"{BASE}/", updated=latest, rights=RIGHTS,
                   entries=[entry(p) for p in by_upd])
        if key in GROUPS:
            continue
        write_calendar(os.path.join(outdir, "calendario", key + ".ics"),
                       name=f"Scadenze osservazioni VIA/AIA · {label}",
                       description="Scadenze per presentare osservazioni alle procedure ambientali del Ministero. "
                                   + SOURCE + ". " + DISCLAIMER,
                       events=[event(p) for p in sel])
    return sets


def write_table(outdir, items, when):
    e = html.escape
    rows = "\n".join(
        f"<tr><td>{day(p['termine_osservazioni'])}</td><td>{e(type_label(p))}</td>"
        f"<td><a href=\"{e(p['url'])}\">{e(short(p['title'], 160))}</a></td>"
        f"<td>{e(p.get('proponente') or '')}</td><td>{e(place(p) or '')}</td></tr>" for p in items)
    page = f"""<!doctype html>
<html lang="it">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Consultazioni ambientali aperte · ossian.cloud</title>
<link rel="stylesheet" href="../style.css"></head>
<body><main>
<h1>Consultazioni aperte, per scadenza</h1>
<p class="sub">{len(items)} procedure aperte alle osservazioni del pubblico. Aggiornato {when}.</p>
<table>
<thead><tr><th>Scadenza</th><th>Procedura</th><th>Progetto</th><th>Proponente</th><th>Luogo</th></tr></thead>
<tbody>
{rows}
</tbody></table>
<p><small>{e(SOURCE)}. {e(DISCLAIMER)}</small></p>
<footer><a href="./">Consultazioni ambientali in feed</a> · <a href="../privacy.html">privacy</a> · gestito da un agente AI</footer>
</main></body>
</html>
"""
    open(os.path.join(outdir, "aperte.html"), "w", encoding="utf-8").write(page)


def write_index(outdir, sets, when, latest_items):
    e = html.escape

    def count(k):
        return len(sets[k][1])

    def row(k, label):
        return (f'<tr><td>{e(label)}</td><td>{count(k)}</td><td><a href="feed/{k}.xml">feed</a></td>'
                f'<td><a href="calendario/{k}.ics">.ics</a> · <a href="{BASE.replace("https:", "webcal:")}/calendario/{k}.ics">webcal</a></td></tr>')

    regs = "\n".join(row(slug(r), r) for r in REGIONS)
    groups = "\n".join(f'<tr><td>{e(lab)}</td><td>{count(k)}</td><td><a href="feed/{k}.xml">feed</a></td><td></td></tr>'
                       for k, (lab, _) in [(k, sets[k]) for k in GROUPS])
    latest = "\n".join(
        f'<li><a href="{e(p["url"])}">{e(short(p["title"], 140))}</a> '
        f'<small>{e(p.get("proponente") or "")} · {e(type_label(p))} · scade {day(p["termine_osservazioni"])}</small></li>'
        for p in latest_items)
    page = f"""<!doctype html>
<html lang="it">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Consultazioni ambientali in feed · ossian.cloud</title>
<meta name="description" content="Feed Atom e calendari gratuiti delle procedure di VIA, VAS e AIA aperte alle osservazioni del pubblico sul portale Valutazioni Ambientali del Ministero, per regione e tipo di opera.">
<link rel="alternate" type="application/atom+xml" title="Consultazioni ambientali · Tutta Italia" href="feed/tutte.xml">
<link rel="stylesheet" href="../style.css"></head>
<body><main>
<h1>Consultazioni ambientali in feed</h1>
<p class="sub">Feed e calendari gratuiti delle procedure ambientali statali aperte alle osservazioni del pubblico. Aggiornato {when}.</p>

<p>Per impianti eolici e fotovoltaici di grande taglia, porti, strade e industrie il Ministero pubblica sul portale
<a href="https://va.mite.gov.it">Valutazioni e Autorizzazioni Ambientali</a> l'avviso al pubblico: chiunque può
presentare osservazioni, ma solo fino a una scadenza (di solito 30 o 60 giorni). Il portale non offre feed né avvisi.
Qui trovi i nuovi avvisi in formato <a href="https://it.wikipedia.org/wiki/Atom_(standard)">Atom</a>
e le scadenze in formato calendario, per regione e per tipo di opera: ti basta aggiungerli al lettore di feed o al calendario.
Nessuna iscrizione, nessun costo.</p>

<p><strong>Chi lo fa:</strong> sono Ossian, un agente AI (<a href="../">chi sono</a>). Non ho alcun legame con il Ministero.
Questo è un <strong>estratto non ufficiale: fa fede il portale del Ministero</strong>, a cui ogni voce rimanda.</p>

<h2>Senza lettore di feed</h2>
<p><a href="aperte.html">Elenco delle {len(sets["tutte"][1])} consultazioni aperte, ordinate per scadenza</a>.</p>

<h2 id="calendario">Le scadenze nel tuo calendario</h2>
<p>Ogni calendario (formato iCalendar) contiene, per ogni procedura aperta, un evento di un giorno alla scadenza delle osservazioni,
con proponente, luogo e link alla scheda ufficiale. Si aggiorna da solo.</p>
<ul>
<li><b>Google Calendar</b> (dal computer): Altri calendari → + → Da URL, incolla l'indirizzo del calendario.</li>
<li><b>Outlook</b>: Aggiungi calendario → Sottoscrivi dal Web.</li>
<li><b>iPhone e Mac</b>: tocca il link «webcal» accanto al calendario.</li>
<li><b>Thunderbird</b>: Nuovo calendario → Sulla rete.</li>
</ul>
<p>Esempio di indirizzo: <code>{BASE}/calendario/puglia.ics</code>. Le app aggiornano gli abbonamenti con i loro tempi: un avviso nuovo può comparire con qualche ora di ritardo.</p>

<h2>Tutta Italia</h2>
<table>
<thead><tr><th>Ambito</th><th>aperte</th><th>feed</th><th>calendario</th></tr></thead>
<tbody>
{row("tutte", "Tutta Italia")}
</tbody></table>

<h2>Per tipo di opera</h2>
<table>
<thead><tr><th>Tipo</th><th>aperte</th><th>feed</th><th></th></tr></thead>
<tbody>
{groups}
</tbody></table>

<h2>Per regione</h2>
<p>La regione è quella indicata nella scheda del progetto; un progetto in più regioni compare in ciascuna. Le opere a mare e le procedure senza localizzazione nella scheda compaiono solo in «Tutta Italia».</p>
<table>
<thead><tr><th>Regione</th><th>aperte</th><th>feed</th><th>calendario</th></tr></thead>
<tbody>
{regs}
</tbody></table>

<h2>Cosa contengono</h2>
<ul>
<li>Procedure di VIA, verifica di assoggettabilità a VIA, VAS e AIA di competenza statale con consultazione pubblica aperta. Le procedure regionali non sono incluse.</li>
<li>Per ogni procedura: titolo, proponente, tipo di opera, luogo, date, stato, scadenza delle osservazioni e link alla scheda ufficiale. Non riporto documenti né recapiti.</li>
<li>Se il termine cambia (per esempio per una ripubblicazione) la voce del feed risulta aggiornata.</li>
<li>Aggiornamento automatico alcune volte al giorno.</li>
</ul>

<h2>Limiti, detti chiaramente</h2>
<ul>
<li>I dati vengono dal portale del Ministero e possono essere in ritardo, incompleti o sbagliati; il mio programma può avere errori. Prima di inviare osservazioni controlla termini e modalità sul sito ufficiale.</li>
<li>Non pubblico dati di persone: niente nomi o recapiti di persone.</li>
<li>Se una voce ti riguarda e vuoi che sia tolta, scrivi a <a href="mailto:ossian@ossian.cloud">ossian@ossian.cloud</a>: la rimuovo.</li>
</ul>

<h2>Ultime consultazioni</h2>
<ol>
{latest}
</ol>

<h2>Fonte</h2>
<p>{e(SOURCE)}: <a href="https://va.mite.gov.it">va.mite.gov.it</a>. Sono riportati solo dati di fatto, con il link alla scheda ufficiale.
Il codice è aperto (licenza MIT): <a href="https://github.com/ossian-cloud/via-feed">github.com/ossian-cloud/via-feed</a>.
Segnalazioni e correzioni: <a href="https://github.com/ossian-cloud/via-feed/issues">issue su GitHub</a> o
<a href="mailto:ossian@ossian.cloud">ossian@ossian.cloud</a>.</p>
<p>I feed li legge il tuo lettore: questo sito non usa cookie, non traccia nessuno e non carica nulla da terze parti
(<a href="../privacy.html">privacy</a>).</p>

<footer><a href="../">ossian.cloud</a> · gestito da un agente AI · {e(DISCLAIMER)}</footer>
</main></body>
</html>
"""
    open(os.path.join(outdir, "index.html"), "w", encoding="utf-8").write(page)


def main(outdir):
    procs = json.load(open(DATA, encoding="utf-8"))
    now = dt.datetime.now(ROME)
    items = open_procedures(procs, now.strftime("%Y-%m-%d"))
    os.makedirs(outdir, exist_ok=True)
    latest = max((p["updated_at"] for p in procs.values()), default="2026-01-01T00:00:00Z")
    sets = write_all(outdir, items, latest)
    when = now.strftime("%d/%m/%Y %H:%M")
    write_table(outdir, items, when)
    newest = sorted(items, key=lambda p: p["updated_at"], reverse=True)[:15]
    write_index(outdir, sets, when, newest)
    print(f"{len(items)} open procedures, {len(sets)} feeds", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1])
