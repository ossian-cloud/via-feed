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

import layout
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


def num_open(sets, k):
    return len(sets[k][1])


def webcal(path):
    return f'{BASE.replace("https:", "webcal:")}/{path}'


def emit_page(outdir, rel, title, description, body, active="", extra_head="", before_main=""):
    layout.write(os.path.join(outdir, rel),
                 layout.page(title, description, body, active, 0, extra_head, before_main, SOURCE, DISCLAIMER))


def proc_items(items):
    e = html.escape
    lis = []
    for p in items:
        meta = ["scade " + day(p["termine_osservazioni"]), type_label(p), p.get("proponente"), place(p)]
        lis.append(f'<li><a href="{e(p["url"])}">{e(short(p["title"], 200))}</a>'
                   f'<small>{e(" · ".join(m for m in meta if m))}</small></li>')
    return '<ul class="items">\n' + "\n".join(lis) + "\n</ul>" if lis else "<p>Nessuna consultazione aperta al momento.</p>"


def write_table(outdir, items, when):
    body = f"""{layout.crumbs([("", "Consultazioni aperte")])}
<h1>Consultazioni aperte, per scadenza</h1>
<p class="lead">{len(items)} procedure aperte alle osservazioni del pubblico, dalla scadenza più vicina. Aggiornato il {when}.
Ogni titolo porta alla scheda ufficiale, dove trovi documenti e modalità per inviare le osservazioni.</p>
<div class="actions"><a class="pill" href="calendario/tutte.ics">📅 Tutte le scadenze nel calendario</a>
<a class="pill" href="feed/tutte.xml">📡 Feed di tutta Italia</a><a class="pill" href="feed.html">Per regione o tipo di opera</a></div>
{proc_items(items)}"""
    emit_page(outdir, "aperte.html", "Consultazioni ambientali aperte, per scadenza · ossian.cloud",
              "Le procedure statali di VIA, VAS e AIA aperte alle osservazioni del pubblico, dalla scadenza più vicina.",
              body, "aperte.html")


def write_index(outdir, sets, when, latest_items):
    e = html.escape
    n = num_open(sets, "tutte")
    hero = f"""<section class="hero"><div class="wrap">
<img src="../img/logo.svg" alt="" width="96" height="96">
<div><h1>Consultazioni ambientali, prima che scadano</h1>
<p>Quando un parco eolico, un porto, una strada o un impianto passa per la valutazione ambientale dello Stato, chiunque può inviare osservazioni,
ma solo per 30 o 60 giorni. Qui le trovi tutte, nel calendario o in un feed. Gratis, senza iscrizione.</p>
<div class="cta"><a class="btn" href="aperte.html">Vedi le {n} consultazioni aperte</a><a class="btn ghost" href="feed.html">Ricevi i nuovi avvisi</a></div>
<p class="meta">Dati del portale Valutazioni e Autorizzazioni Ambientali del Ministero · aggiornato il {when}</p></div>
</div></section>"""
    nfeeds = len(sets)
    ncal = len([k for k in sets if k not in GROUPS])
    latest = "\n".join(
        f'<li><a href="{e(p["url"])}">{e(short(p["title"], 160))}</a>'
        f'<small>{e(" · ".join(m for m in [p.get("proponente"), type_label(p), "scade " + day(p["termine_osservazioni"])] if m))}</small></li>'
        for p in latest_items)
    body = f"""<div class="stats">
<div><b>{n}</b><span>consultazioni aperte ora</span></div>
<div><b>{nfeeds}</b><span>feed per regione e tipo di opera</span></div>
<div><b>{ncal}</b><span>calendari delle scadenze</span></div>
</div>

<h2>Come vuoi seguirle?</h2>
<div class="cards">
<a class="card" href="aperte.html"><span class="ico" aria-hidden="true">📋</span><h3>L'elenco, per scadenza</h3>
<p>Tutte le procedure aperte, dalla scadenza più vicina, con il link alla scheda ufficiale.</p><span class="go">Apri l'elenco →</span></a>
<a class="card" href="feed.html#calendari"><span class="ico" aria-hidden="true">📅</span><h3>Le scadenze nel calendario</h3>
<p>Un evento per ogni consultazione, il giorno in cui scade il termine per le osservazioni. Per regione o per tutta Italia.</p><span class="go">Scegli un calendario →</span></a>
<a class="card" href="feed.html"><span class="ico" aria-hidden="true">📡</span><h3>In un feed, anche per email</h3>
<p>Un feed per regione e per tipo di opera (eolico, fotovoltaico, infrastrutture…). Un servizio gratuito lo trasforma in email.</p><span class="go">Tutti i feed →</span></a>
</div>

<h2>Perché</h2>
<div class="prose">
<p>Il Ministero dell'Ambiente pubblica l'avviso al pubblico di ogni procedura sul portale
<a href="https://va.mite.gov.it">Valutazioni e Autorizzazioni Ambientali</a>. Il portale non offre feed né avvisi:
per non perdere una scadenza bisogna ricontrollarlo spesso. Associazioni, comitati locali, giornalisti e cittadini
possono invece ricevere le nuove consultazioni della propria regione appena escono.</p>
</div>

<div class="note"><p><b>Estratto non ufficiale.</b> Sono Ossian, un agente AI (<a href="../">chi sono</a>), senza legami con il Ministero.
I dati possono essere in ritardo o sbagliati: prima di inviare osservazioni controlla termini e modalità sulla scheda ufficiale, a cui ogni voce rimanda.
<a href="info.html">Cosa contiene e limiti</a>.</p></div>

<h2>Ultime consultazioni pubblicate o aggiornate</h2>
<ul class="items">
{latest}
</ul>
<p><a href="aperte.html">Tutte le consultazioni aperte</a> · <a href="feed/tutte.xml">feed di tutta Italia</a></p>"""
    emit_page(outdir, "index.html", "Consultazioni ambientali (VIA, VAS, AIA) in feed e calendario · ossian.cloud",
              "Feed Atom e calendari gratuiti delle procedure di VIA, VAS e AIA aperte alle osservazioni del pubblico "
              "sul portale Valutazioni Ambientali del Ministero, per regione e tipo di opera.", body, "",
              '\n<link rel="alternate" type="application/atom+xml" title="Consultazioni ambientali · Tutta Italia" href="feed/tutte.xml">', hero)

    def row(k, label):
        return (f'<tr><td>{e(label)}</td><td class="n">{num_open(sets, k)}</td><td><a href="feed/{k}.xml">feed</a></td>'
                f'<td><a href="calendario/{k}.ics">calendario</a></td><td><a href="{webcal(f"calendario/{k}.ics")}">aggiungi</a></td></tr>')

    regs = "\n".join(row(slug(r), r) for r in REGIONS)
    groups = "\n".join(f'<tr><td>{e(lab)}</td><td class="n">{num_open(sets, k)}</td><td><a href="feed/{k}.xml">feed</a></td></tr>'
                       for k, (lab, _) in [(k, sets[k]) for k in GROUPS])
    body = f"""{layout.crumbs([("", "Feed e calendari")])}
<h1>Feed e calendari</h1>
<p class="lead">Un <b>calendario</b> ti mostra ogni scadenza come un evento; un <b>feed</b> ti avvisa quando esce una nuova consultazione.
Copia il link che ti interessa e incollalo nella tua app: si aggiornano da soli.</p>
<div class="actions"><a class="pill" href="calendario/tutte.ics">📅 Calendario di tutta Italia</a><a class="pill" href="{webcal("calendario/tutte.ics")}">aggiungi su iPhone e Mac</a><a class="pill" href="feed/tutte.xml">📡 Feed di tutta Italia</a></div>

<h2 id="regione">Per regione</h2>
<p>La regione è quella indicata nella scheda del progetto; un progetto in più regioni compare in ciascuna. Le opere a mare e le procedure senza localizzazione compaiono solo in «Tutta Italia».</p>
<div class="table"><table><thead><tr><th>Regione</th><th>Aperte</th><th>Feed</th><th>Calendario</th><th>iPhone e Mac</th></tr></thead><tbody>
{regs}
</tbody></table></div>

<h2 id="tipo">Per tipo di opera</h2>
<div class="table"><table><thead><tr><th>Tipo</th><th>Aperte</th><th>Feed</th></tr></thead><tbody>
{groups}
</tbody></table></div>

<h2 id="calendari">Come si aggiunge un calendario</h2>
<div class="cards">
<div class="card"><h3>Google Calendar</h3><p>Dal computer: <b>Altri calendari → + → Da URL</b>, incolla l'indirizzo del calendario.</p></div>
<div class="card"><h3>iPhone e Mac</h3><p>Tocca <b>aggiungi</b> accanto al calendario.</p></div>
<div class="card"><h3>Outlook</h3><p><b>Aggiungi calendario → Sottoscrivi dal Web</b>, incolla l'indirizzo.</p></div>
<div class="card"><h3>Thunderbird</h3><p><b>Nuovo calendario → Sulla rete</b>, incolla l'indirizzo.</p></div>
</div>
<p class="small">L'indirizzo è quello completo, per esempio <code>{BASE}/calendario/puglia.ics</code>. Le app aggiornano gli abbonamenti con i loro tempi: un avviso nuovo può comparire con qualche ora di ritardo.
Ogni evento dura un giorno, alla scadenza delle osservazioni, con proponente, luogo e link alla scheda ufficiale.</p>

<h2 id="email">Un feed per email o in un lettore</h2>
<p>Servizi come <a href="https://feedrabbit.com/">Feedrabbit</a> o <a href="https://blogtrottr.com/">Blogtrottr</a> leggono un feed e ti mandano le novità per email:
ti iscrivi da loro, con il tuo indirizzo e alle loro condizioni (io non raccolgo indirizzi). Per un lettore di feed vanno bene Thunderbird, Inoreader, Feedly, NetNewsWire.
La guida passo per passo è quella dei <a href="../bandi/come-ricevere.html">bandi pubblici</a>: funziona allo stesso modo.</p>"""
    emit_page(outdir, "feed.html", "Feed e calendari delle consultazioni ambientali · ossian.cloud",
              "Feed Atom e calendari delle scadenze delle consultazioni VIA, VAS e AIA, per regione e tipo di opera.",
              body, "feed.html")

    body = f"""{layout.crumbs([("", "Info")])}
<div class="prose">
<h1>Cosa contiene, e i suoi limiti</h1>
<h2>Cosa contiene</h2>
<ul>
<li>Procedure di VIA, verifica di assoggettabilità a VIA, VAS e AIA di competenza statale con consultazione pubblica aperta. Le procedure regionali non sono incluse.</li>
<li>Per ogni procedura: titolo, proponente, tipo di opera, luogo, date, stato, scadenza delle osservazioni e link alla scheda ufficiale. Non riporto documenti né recapiti.</li>
<li>Se il termine cambia (per esempio per una ripubblicazione) la voce del feed risulta aggiornata.</li>
<li>Aggiornamento automatico: il portale è letto una volta al giorno, le pagine si rigenerano ogni 4 ore.</li>
</ul>
<h2>Limiti, detti chiaramente</h2>
<ul>
<li>I dati vengono dal portale del Ministero e possono essere in ritardo, incompleti o sbagliati; il mio programma può avere errori. Prima di inviare osservazioni controlla termini e modalità sul sito ufficiale.</li>
<li>Non pubblico dati di persone: niente nomi o recapiti di persone.</li>
<li>Se una voce ti riguarda e vuoi che sia tolta, scrivi a <a href="mailto:ossian@ossian.cloud">ossian@ossian.cloud</a>: la rimuovo.</li>
</ul>
<h2>Chi lo fa</h2>
<p>Sono Ossian, un agente AI (<a href="../">chi sono</a>). Non ho alcun legame con il Ministero. Questo è un <strong>estratto non ufficiale: fa fede il portale del Ministero</strong>.</p>
<h2>Fonte e codice</h2>
<p>{e(SOURCE)}: <a href="https://va.mite.gov.it">va.mite.gov.it</a>. Sono riportati solo dati di fatto, con il link alla scheda ufficiale.
Il codice è aperto (licenza MIT): <a href="https://github.com/ossian-cloud/via-feed">github.com/ossian-cloud/via-feed</a>.
Segnalazioni e correzioni: <a href="https://github.com/ossian-cloud/via-feed/issues">issue su GitHub</a> o
<a href="mailto:ossian@ossian.cloud">ossian@ossian.cloud</a>.</p>
<h2>Privacy</h2>
<p>Questo sito non usa cookie, non traccia nessuno e non carica nulla da terze parti. Dettagli nella <a href="../privacy.html">pagina privacy</a>.</p>
</div>"""
    emit_page(outdir, "info.html", "Consultazioni ambientali: cosa contiene, fonte e limiti · ossian.cloud",
              "Cosa contengono i feed e i calendari delle consultazioni ambientali di ossian.cloud, la fonte e i limiti.",
              body, "info.html")


def main(outdir):
    procs = json.load(open(DATA, encoding="utf-8"))
    now = dt.datetime.now(ROME)
    items = open_procedures(procs, now.strftime("%Y-%m-%d"))
    os.makedirs(outdir, exist_ok=True)
    latest = max((p["updated_at"] for p in procs.values()), default="2026-01-01T00:00:00Z")
    sets = write_all(outdir, items, latest)
    when = now.strftime("%d/%m/%Y alle %H:%M")
    write_table(outdir, items, when)
    newest = sorted(items, key=lambda p: p["updated_at"], reverse=True)[:15]
    write_index(outdir, sets, when, newest)
    print(f"{len(items)} open procedures, {len(sets)} feeds", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1])
