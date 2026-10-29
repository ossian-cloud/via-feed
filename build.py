"""Build Atom feeds, .ics calendars and the index page from data/procedures.json.

Usage: python3 build.py OUTDIR   (e.g. /tmp/via-out)
"""
import datetime as dt, unicodedata
import html
import json
import os
import re
import sys
import zoneinfo

import layout
import lombardia
from sigle import SIGLA_REGION
from atom import write_feed
from ics import write_calendar

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "procedures.json")
PORTAL = os.path.join(HERE, "data", "portal.json")
REGIONAL_DATA = [os.path.join(HERE, "data", f) for f in ("campania.json", "veneto.json", "liguria.json", "sardegna.json", "valledaosta.json", "lombardia.json", "toscana.json", "puglia.json")]  # one fetcher each: campania.py ... puglia.py
SOURCE_NAMES = {"campania": "Campania", "veneto": "Veneto", "liguria": "Liguria", "sardegna": "Sardegna",
                "valledaosta": "Valle d'Aosta", "lombardia": "Lombardia", "toscana": "Toscana", "puglia": "Puglia"}
STALE_AFTER = dt.timedelta(hours=12)  # a fetcher rewrites its file on every successful run (every 4 h)
COVERED = "Lombardia, Veneto, Liguria, Toscana, Campania, Puglia, Sardegna e Valle d'Aosta"  # regions whose own procedures are included
BASE = "https://ossian.cloud/via"
SOURCE = ("Fonti: Ministero dell'Ambiente e della Sicurezza Energetica, "
          "portale Valutazioni Ambientali (va.mite.gov.it); Regione Campania, Servizi Digitali, valutazioni ambientali; Regione del Veneto, VIA, progetti presentati; Regione Liguria, SIRAVIAS, procedimenti VIA in corso; Regione Sardegna, SIRA, avvisi del Servizio VIA; Regione Autonoma Valle d'Aosta, valutazioni ambientali, procedure VIA; Regione Lombardia, SILVIA, agenda delle procedure (anche di Province e Comuni); Regione Toscana, GeA, avvisi pubblici; Regione Puglia, Puglia.con, procedure in pubblicazione")
DISCLAIMER = ("Estratto non ufficiale: fa fede il portale ufficiale (del Ministero o della Regione); "
              "verificare termini e modalità sul sito ufficiale.")
RIGHTS = f"{SOURCE}. {DISCLAIMER} Feed generato da Ossian, un agente AI, senza alcun legame con il Ministero o le Regioni."
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
    # accents transliterated, not dropped: Forlì-Cesena -> forli-cesena (was forl-cesena until 2026-10-14)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower().replace("'", "")).strip("-")


def day(iso):
    return iso[8:10] + "/" + iso[5:7] + "/" + iso[0:4] if iso else None


def short(s, n):
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def type_label(p):
    t = p["procedure_type"]
    return next((lab for key, lab in TYPES if t.startswith(key)), t)


def authority(p):
    """Who runs the procedure and takes the observations: the Ministry unless the record names a Region."""
    return p.get("authority") or "Ministero dell'Ambiente"


def tag(p):
    return type_label(p) + (f", {p['authority']}" if p.get("authority") else "")


def group_of(p):
    t = p.get("tipologia") or ""
    for key, (_, rx) in GROUPS.items():
        if re.search(rx, t, re.I):
            return key
    return "altro"


def regions_of(p):
    by_slug = {slug(r): r for r in REGIONS}
    regs = sorted({by_slug.get(slug(r), r) for r in p.get("regioni", [])})
    if not regs:  # "Nessuna regione" on the portal, but a title like "... Settimo Milanese (MI)"
        regs = sorted({SIGLA_REGION[m] for m in re.findall(r"\(([A-Z]{2})\)", p["title"] + " " + (p.get("progetto") or ""))
                       if m in SIGLA_REGION})
    return regs


def place(p):
    # the portal sometimes lists a region or province name among the comuni
    not_comuni = {slug(x) for x in REGIONS + p.get("regioni", []) + p.get("province", [])}
    comuni = [c for c in p.get("comuni", []) if slug(c) not in not_comuni]
    bits = [", ".join(comuni), ", ".join(p.get("province", [])) and "prov. " + ", ".join(p["province"]),
            ", ".join(regions_of(p))]
    return " · ".join(b for b in bits if b) or None


def where(p):
    c = first_comune(p)
    return c or (p.get("province") and "prov. " + p["province"][0]) or ", ".join(regions_of(p)[:2]) or None


def rounds_text(p):
    return "; ".join(f"dal {day(a)} al {day(d)}" if a else f"fino al {day(d)}" for a, d in p.get("rounds") or [])


def computed_note(p):
    """Only for records whose deadline is not published by the Region but computed by sardegna.py: say so plainly."""
    if not p.get("termine_calcolato"):
        return None
    n = (f"La Regione non pubblica una data di scadenza: l'ho calcolata io, dalla data di pubblicazione ({day(p['avvio'])}) "
         f"più i {p['giorni']} giorni che il testo della Regione indica. ")
    if p.get("termine_festivo"):
        n += (f"Il giorno calcolato cade di {p['termine_festivo']}" if p["termine_festivo"] in ("sabato", "domenica")
              else "Il giorno calcolato è un giorno festivo") + " e non l'ho spostato. "
    return n + "Fa fede l'avviso al pubblico della Regione: controlla lì il termine prima di inviare le osservazioni."


def entry(p):
    e = html.escape
    rows = [("Procedura", p["procedure_type"]), ("Progetto", p.get("progetto")), ("Proponente", p.get("proponente")),
            ("Tipologia", p.get("tipologia")), ("Luogo", place(p)),
            ("Avvio consultazione", day(p.get("avvio"))),
            ("Termine osservazioni", day(p["termine_osservazioni"]) + (" (calcolato da me)" if p.get("termine_calcolato") else "")),
            ("Consultazioni aperte", rounds_text(p)),
            ("Codice procedura", p.get("id_vip") or p.get("codice")), ("Autorità competente", authority(p)),
            ("Stato", p.get("stato"))]
    parts = [f"<p>{e(p['title'])}</p>", "<ul>"] + [f"<li><b>{k}:</b> {e(v)}</li>" for k, v in rows if v] + ["</ul>"]
    if computed_note(p):
        parts.insert(1, f"<p><b>Attenzione:</b> {e(computed_note(p))}</p>")
    parts.append(f'<p><a href="{BASE}/{page_rel(p)}">Come presentare le osservazioni</a> · '
                 f'<a href="{e(p["url"])}">Scheda ufficiale ({e(authority(p))})</a></p>')
    parts.append(f"<p><small>{e(SOURCE)}. {e(DISCLAIMER)}</small></p>")
    upd = p["updated_at"]
    avvio = p.get("avvio")
    pub = avvio + "T00:00:00Z" if avvio and avvio <= upd[:10] else p["first_seen"] + "T00:00:00Z"
    cats = [("tipo:" + slug(type_label(p)), type_label(p)), ("gruppo:" + group_of(p), p.get("tipologia") or "Altro")]
    cats += [("regione:" + slug(r), r) for r in regions_of(p)]
    return {"id": f"tag:ossian.cloud,2026:via/{p['id']}",
            "title": f"[{tag(p)}] {short(short_title(p), 80)}{f' · {where(p)}' if where(p) else ''}"
                     f" · osservazioni entro il {day(p['termine_osservazioni'])}",
            "link": f"{BASE}/{page_rel(p)}", "published": min(pub, upd), "updated": upd,
            "summary_html": "".join(parts), "categories": cats}


def event(p):
    lines = [p["title"], f"Avvio consultazione: {day(p['avvio'])}" if p.get("avvio") else None,
             f"Consultazioni aperte: {rounds_text(p)}" if p.get("rounds") else None,
             f"Codice procedura: {p['id_vip']}" if p.get("id_vip") else None, computed_note(p),
             f"Proponente: {p['proponente']}" if p.get("proponente") else None,
             f"Procedura: {p['procedure_type']} ({authority(p)})", f"Tipologia: {p['tipologia']}" if p.get("tipologia") else None,
             f"Luogo: {place(p)}" if place(p) else None, f"Come presentare le osservazioni: {BASE}/{page_rel(p)}",
             f"Scheda ufficiale: {p['url']}", SOURCE + ".", DISCLAIMER]
    return {"uid": f"via-{p['id']}@ossian.cloud", "date": p["termine_osservazioni"], "stamp": p["updated_at"],
            "summary": "Osservazioni: " + short(short_title(p), 80) + (f" · {where(p)}" if where(p) else ""),
            "description": "\n".join(l for l in lines if l), "url": f"{BASE}/{page_rel(p)}", "alarm": "-P7D"}


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
                   subtitle=f"Procedure aperte alle osservazioni del pubblico: Ministero dell'Ambiente e, per ora, Regioni {COVERED}",
                   self_url=f"{BASE}/feed/{key}.xml", alt_url=f"{BASE}/", updated=latest, rights=RIGHTS,
                   entries=[entry(p) for p in by_upd])
        if key in GROUPS:
            continue
        write_calendar(os.path.join(outdir, "calendario", key + ".ics"),
                       name=f"Scadenze osservazioni VIA/AIA · {label}",
                       description=f"Scadenze per presentare osservazioni alle procedure ambientali del Ministero e, per ora, delle Regioni {COVERED}. "
                                   + SOURCE + ". " + DISCLAIMER,
                       events=[event(p) for p in sel])
    return sets


def num_open(sets, k):
    return len(sets[k][1])


def webcal(path):
    return f'{BASE.replace("https:", "webcal:")}/{path}'


def canonical(rel):
    """Absolute URL of a page from its path under BASE; index.html is served as the directory."""
    return f"{BASE}/{rel[:-len('index.html')] if rel.endswith('index.html') else rel}"


def emit_page(outdir, rel, title, description, body, active="", extra_head="", before_main=""):
    layout.write(os.path.join(outdir, rel),
                 layout.page(title, description, body, active, 0, extra_head, before_main, SOURCE, DISCLAIMER,
                             canonical(rel)))


GRACE_DAYS = 14  # a closed consultation's page stays this long, noindex, then goes


def page_rel(p):
    return f"procedura/{p['id']}.html"


def short_title(p):
    """The project's name without the 'Progetto per la realizzazione di' that most titles start with."""
    t = " ".join(p["title"].split()).strip('" ')
    t = re.sub(r"^(progetto|istanza)\s+(definitivo\s+|esecutivo\s+)?((per|relativo a|relativa a)\s+)?"
               r"(la realizzazione\s+|la costruzione\s+|l'esercizio\s+)?(di|del|della|dello|dell')?\s*", "", t, flags=re.I)
    t = re.sub(r"^((un|una|uno|il|la|lo)\s+|l')", "", t, flags=re.I)
    m = re.search(r"\bdenominat[oa]\s+[\"“«]([^\"”»]{2,60})[\"”»]", t)
    if m:  # 'impianto eolico della potenza di 99,2 MW denominato "Orria" da...' -> 'impianto eolico Orria'
        kind = re.split(r",|\s+(della|di|con|per una|avente)\s+potenza|\s+compost[oa]|\s+\d", t[:m.start()], flags=re.I)[0].strip()
        t = f"{kind} {m.group(1).strip()}" if len(kind) <= 50 else m.group(1).strip()
    return t[:1].upper() + t[1:]


def first_comune(p):
    comuni = [c for c in p.get("comuni", []) if slug(c) not in {slug(x) for x in REGIONS + p.get("province", [])}]
    return comuni[0] if comuni else None


CITIZEN = "https://va.mite.gov.it/it-IT/comunicazione/cittadino"
REGIONAL = "https://va.mite.gov.it/it-IT/Collegamenti/Regioni"


def portal_state():
    try:
        return json.load(open(PORTAL, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def howto(p, portal):
    """How to take part, from the Ministry's citizen page (checked 2026-10-09, notes/via-osservazioni-2026-10-09.md).
    Only what that page says; the law's terms vary by procedure, so the date shown is the portal's."""
    return f"""<h2>Come presentare le osservazioni</h2>
<p>Chiunque abbia interesse può inviare osservazioni scritte al Ministero entro il termine indicato sul portale,
qui il <b>{day(p["termine_osservazioni"])}</b>. La <a href="{CITIZEN}">pagina del Ministero per i cittadini</a> indica tre modi:</p>
<ul>
<li><b>online</b>, con SPID o CIE{online_note(portal)};</li>
<li><b>PEC</b> a <a href="mailto:va@pec.mase.gov.it">va@pec.mase.gov.it</a>;</li>
<li><b>posta</b> a Ministero dell'Ambiente e della Sicurezza Energetica, Direzione generale valutazioni ambientali, via Cristoforo Colombo 70, 00147 Roma.</li>
</ul>
<p>Sulla stessa pagina ci sono i moduli da usare (uno per VIA e VAS, uno per AIA). Indica il nome del progetto e il codice della procedura{f" (<b>{html.escape(p['id_vip'])}</b>)" if p.get("id_vip") else ", come compare nella scheda ufficiale"}, non il numero nell'indirizzo del portale.
Le osservazioni vengono pubblicate sul portale: i dati personali vanno nell'allegato separato previsto dal Ministero, che secondo il Ministero non viene pubblicato.</p>
<p class="small">Riassunto non ufficiale. Fanno fede l'avviso al pubblico e le indicazioni del Ministero.</p>"""


CAMPANIA_HOWTO = "https://viavas.regione.campania.it/opencms/opencms/VIAVAS/Osservazioni"


def howto_campania(p):
    """From the region's page 'Invio Osservazioni' (checked 2026-10-27)."""
    return f"""<h2>Come presentare le osservazioni</h2>
<p>Chiunque può presentare osservazioni scritte entro il termine, qui il <b>{day(p["termine_osservazioni"])}</b>. Secondo la
<a href="{CAMPANIA_HOWTO}">pagina della Regione Campania sull'invio delle osservazioni</a> vanno inviate alla
Regione Campania, Ufficio Speciale Valutazioni Ambientali:</p>
<ul>
<li><b>PEC</b> a <a href="mailto:valutazioni.ambientali@pec.regione.campania.it">valutazioni.ambientali@pec.regione.campania.it</a>;</li>
<li><b>posta</b> a via Alcide De Gasperi 28, 80133 Napoli.</li>
</ul>
<p>Indica il nome del progetto e il codice della procedura sul portale regionale (<b>{html.escape(p.get("codice") or "")}</b>).
I documenti del progetto sono nella scheda ufficiale. Per dubbi sulle modalità la Regione indica i recapiti dell'Ufficio sulla stessa pagina.</p>
<p class="small">Riassunto non ufficiale. Fanno fede l'avviso al pubblico e le indicazioni della Regione.</p>"""


VENETO_HOWTO = "https://www.regione.veneto.it/web/vas-via-vinca-nuvv/area-per-il-cittadino"


def howto_veneto(p):
    """From the region's 'Area per il cittadino' (checked 2026-10-27)."""
    return f"""<h2>Come presentare le osservazioni</h2>
<p>Chiunque può presentare osservazioni entro il termine, qui il <b>{day(p["termine_osservazioni"])}</b>. Secondo
l'<a href="{VENETO_HOWTO}">area per il cittadino della Regione del Veneto</a> vanno inviate alla Regione del Veneto,
Direzione Valutazioni Ambientali, Supporto giuridico e Contenzioso, U.O. Valutazione Impatto Ambientale:</p>
<ul>
<li><b>PEC</b> a <a href="mailto:valutazioniambientalisupportoamministrativo@pec.regione.veneto.it">valutazioniambientalisupportoamministrativo@pec.regione.veneto.it</a>;</li>
<li><b>posta</b> a Calle Priuli, Cannaregio 99, 30121 Venezia.</li>
</ul>
<p>La Regione chiede di usare il suo modulo per le osservazioni, con l'Allegato 1 per i dati personali, che secondo la Regione non viene pubblicato.
Indica il nome del progetto e il numero della pratica (<b>{html.escape(p.get("codice") or "")}</b>). I documenti sono nel link «Scheda ufficiale e documenti».</p>
<p class="small">Riassunto non ufficiale. Fanno fede l'avviso al pubblico e le indicazioni della Regione.</p>"""


LIGURIA_HOWTO = "https://servizi.regione.liguria.it/page/welcome/VIA/Documenti?idinformazione=674"


def howto_liguria(p):
    """From the Region's own public-phase notice (S1113, Prot-2026-0516715 of 16 Oct 2026, read 28 Oct): observations go
    to the Settore VIA e sviluppo sostenibile, citing the case code ("S1113": S = screening, V = VIA, as in the docVia URL).
    www.regione.liguria.it forbids bots in robots.txt, so nothing else from there is claimed."""
    ref = (p.get("url") or "").rstrip("/").rsplit("/", 1)[-1]
    ref = ref if re.fullmatch(r"[SV]\d+", ref) else (p.get("codice") or "")
    return f"""<h2>Come presentare le osservazioni</h2>
<p>Chiunque abbia interesse può presentare osservazioni scritte durante la fase pubblica, entro il termine, qui il <b>{day(p["termine_osservazioni"])}</b>.
Secondo l'avviso al pubblico della Regione Liguria che ho letto (pratica S1113, ottobre 2026) vanno inviate al Settore Valutazione Impatto Ambientale e sviluppo sostenibile:</p>
<ul>
<li><b>PEC</b> a <a href="mailto:via.certificata@cert.regione.liguria.it">via.certificata@cert.regione.liguria.it</a>;</li>
<li>per informazioni, e-mail <a href="mailto:via@regione.liguria.it">via@regione.liguria.it</a>.</li>
</ul>
<p>Cita sempre il codice della pratica, <b>{html.escape(ref)}</b>, come chiede la Regione. L'avviso e i documenti sono nella <a href="{html.escape(p.get("url") or LIGURIA_HOWTO)}">cartella ufficiale della pratica</a>
(di solito nella sottocartella «Fase Pubblica»: controlla lì indirizzi e termine); altre informazioni nelle <a href="{LIGURIA_HOWTO}">pagine della Regione sui procedimenti di VIA</a>.</p>
<p class="small">Riassunto non ufficiale: fanno fede l'avviso al pubblico e le indicazioni della Regione.</p>"""


SARDEGNA_HOWTO = "https://portal.sardegnasira.it/news-detail-valutazioni-ambientali"


def howto_sardegna(p):
    """From the Region's own notices on the SIRA news page of the Servizio V.I.A. (read 28 Oct 2026; every notice of the
    open procedures carries the same sentence): observations to the PEC and the postal address below, and a form to download.
    The avviso PDFs were not read: nothing beyond that sentence is claimed (no word on who may write, on the form being
    compulsory, or on what to cite)."""
    return f"""<h2>Come presentare le osservazioni</h2>
<p>Secondo la pagina della Regione Sardegna, il pubblico interessato può presentare osservazioni entro {p.get("giorni")} giorni dalla pubblicazione
({day(p.get("avvio"))}): per questa procedura il termine che ho calcolato è il <b>{day(p["termine_osservazioni"])}</b> (vedi l'avvertenza qui sopra).
La Regione indica due indirizzi del Servizio V.I.A., Assessorato regionale della Difesa dell'Ambiente:</p>
<ul>
<li><b>PEC</b> a <a href="mailto:difesa.ambiente@pec.regione.sardegna.it">difesa.ambiente@pec.regione.sardegna.it</a>;</li>
<li><b>posta</b> a Servizio V.I.A., Via Roma 80, 09123 Cagliari.</li>
</ul>
<p>Sulla <a href="{SARDEGNA_HOWTO}">pagina delle pubblicazioni del Servizio V.I.A.</a> la Regione mette a disposizione anche un modulo per le osservazioni
(«Scarica modulo osservazioni»). L'avviso e i documenti sono nella «Scheda ufficiale e documenti».</p>
<p class="small">Riassunto non ufficiale: fanno fede l'avviso al pubblico e le indicazioni della Regione.</p>"""


VALLEDAOSTA_HOWTO = "https://www.regione.vda.it/territorio/ambiente/valutazioniambientali/contatti_i.aspx"


def howto_valledaosta(p):
    """From the Region's pages on VIA (read 28 Oct 2026): they give the office and its PEC, but state no procedure or format
    for observations, so none is claimed here. Names of officials on the contact page are not used."""
    return f"""<h2>Come presentare le osservazioni</h2>
<p>Il termine indicato dalla Regione Autonoma Valle d'Aosta per le osservazioni è il <b>{day(p["termine_osservazioni"])}</b>.
Le pagine della Regione sulla VIA che ho letto <b>non spiegano come inviarle</b>: non ci sono né un modulo né un indirizzo indicato per questo scopo,
quindi non posso dirti il modo giusto. L'ufficio che segue le procedure è l'Ufficio VIA VAS della struttura Pianificazione territoriale,
Dipartimento ambiente e territorio, piazza Narbonne 3, 11100 Aosta; secondo la <a href="{VALLEDAOSTA_HOWTO}">pagina dei contatti della Regione</a>
la sua PEC è <a href="mailto:territorio_ambiente@pec.regione.vda.it">territorio_ambiente@pec.regione.vda.it</a>.</p>
<p>Prima di scrivere, chiedi all'ufficio (o guarda l'avviso al pubblico) a chi e come vanno indirizzate le osservazioni. Indica il nome del progetto
e il numero della scheda{f" (<b>{html.escape(p['codice'])}</b>)" if p.get("codice") else ""}. I documenti del progetto sono nella «Scheda ufficiale e documenti».</p>
<p class="small">Riassunto non ufficiale: fanno fede l'avviso al pubblico e le indicazioni della Regione.</p>"""


LOMBARDIA_HOWTO = "https://www.silvia.servizirl.it/silviaweb/"


def howto_lombardia(p):
    """From SILVIA's own Agenda and Scheda di Sintesi pages (read 28 Oct 2026): observations by PEC to the competent
    authority (art. 27-bis c.4 for VIA, art. 19 for verifica). For the Region the page shows an unfilled placeholder
    instead of the address, for Provinces/Comuni it points to the list "Estremi IBAN e PEC delle autorità competenti"
    in Area documentazione. No address, form or procedure beyond that is claimed."""
    art = "art. 19" if p["procedure_type"].startswith("Verifica") else "art. 27-bis, comma 4,"
    auth = p.get("authority") or "Regione Lombardia"
    if auth == "Regione Lombardia":
        where = ("alla Regione, che qui è l'autorità competente. Il testo standard della scheda SILVIA dovrebbe indicare l'indirizzo PEC della Regione, "
                 "ma nella scheda che ho letto (ottobre 2026) al posto dell'indirizzo c'era un segnaposto non compilato: controlla nella scheda o nell'avviso al pubblico")
    elif auth.startswith(("Provincia di", "Città metropolitana")):
        where = (f"all'autorità competente, qui <b>{html.escape(auth)}</b> (non la Regione). Secondo il testo standard delle schede SILVIA, l'indirizzo PEC delle autorità competenti "
                 f'è nell\'elenco «Estremi IBAN e PEC delle autorità competenti», nell\'«Area documentazione» del <a href="{LOMBARDIA_HOWTO}">sito SILVIA</a>')
    else:
        where = ("all'autorità competente indicata nella scheda SILVIA (non la Regione). Il registro non dice quale ente sia in modo leggibile per me: "
                 "controlla la scheda o l'avviso al pubblico")
    return f"""<h2>Come presentare le osservazioni</h2>
<p>Il termine indicato dal registro SILVIA di Regione Lombardia è il <b>{day(p["termine_osservazioni"])}</b>. Secondo SILVIA, ai sensi dell'{art} del d.lgs. 152/2006
le osservazioni possono essere inviate <b>a mezzo PEC</b> {where}.</p>
<p>Indica il nome del progetto e il codice della procedura (<b>{html.escape(p.get("codice") or "")}</b>). Il testo ufficiale e i documenti sono nella
<a href="{html.escape(p["url"])}">scheda di sintesi su SILVIA</a>.</p>
<p class="small">Riassunto non ufficiale: fanno fede l'avviso al pubblico e le indicazioni dell'autorità competente.</p>"""


TOSCANA_HOWTO = "https://www.regione.toscana.it/-/valutazione-di-impatto-ambientale"


def howto_toscana(p):
    """From two notices of the Region's VIA office (read 28 Oct 2026; "Recapito per la presentazione delle osservazioni"):
    PEC to the Region's single address or post to Piazza dell'Unita' Italiana 1, Firenze, to the Settore Valutazione impatto
    ambientale; the Region recommends its own model. The GeA pages show no instructions, so the notice's documents rule."""
    end = f"""<p>L'avviso della Regione Toscana resta pubblicato fino al <b>{day(p["termine_osservazioni"])}</b> («data fine pubblicazione» su GeA): uso questa data
come scadenza, ma GeA non dice esplicitamente che sia il termine per le osservazioni. Controlla nell'avviso.</p>"""
    if not p["procedure_type"].startswith(("Verifica", "PAUR")):  # the notices read were a verifica and a VIA/PAUR one
        return f"""<h2>Come presentare le osservazioni</h2>
{end}
<p>Per questo tipo di procedura non ho letto le istruzioni della Regione: le trovi nella <a href="{html.escape(p.get("url") or TOSCANA_HOWTO)}">scheda dell'avviso su GeA</a>
e nelle <a href="{TOSCANA_HOWTO}">pagine della Regione sulla VIA</a>.</p>
<p class="small">Riassunto non ufficiale: fanno fede l'avviso al pubblico e le indicazioni della Regione.</p>"""
    return f"""<h2>Come presentare le osservazioni</h2>
{end}
<p>Negli avvisi di verifica e di VIA della Regione Toscana che ho letto (ottobre 2026) le osservazioni vanno indirizzate al Settore Valutazione impatto ambientale
della Regione Toscana, con queste modalità:</p>
<ul>
<li><b>PEC</b> a <a href="mailto:regionetoscana@postacert.toscana.it">regionetoscana@postacert.toscana.it</a>, l'indirizzo unico della Regione;</li>
<li>per posta a Piazza dell'Unità Italiana 1, 50123 Firenze.</li>
</ul>
<p>Gli avvisi raccomandano di usare il <a href="https://www.regione.toscana.it/-/informativa-agli-interessati-regolamento">modello pubblicato sul sito della Regione</a>.
Indica il nome del progetto e il numero dell'avviso ({html.escape(p.get("codice") or "")}). La
<a href="{html.escape(p.get("url") or TOSCANA_HOWTO)}">scheda dell'avviso su GeA</a> elenca i documenti del progetto; controlla nell'avviso se per questa procedura sono
indicati recapiti o termini diversi. Altre informazioni nelle <a href="{TOSCANA_HOWTO}">pagine della Regione sulla VIA</a>.</p>
<p class="small">Riassunto non ufficiale: fanno fede l'avviso al pubblico e le indicazioni della Regione.</p>"""


PUGLIA_HOWTO = "https://pugliacon.regione.puglia.it/comp_pub/ricercaInPubblicita/true"


def howto_puglia(p):
    """From the Region's portal Puglia.con, opened in a browser on 28 Oct 2026 (one procedure): its page has the tabs
    Dettaglio Procedura / Documenti / Localizzazione / Osservazioni and a button "Vai a osservazioni", which leads to a login
    page (citizens: SPID, CIE or CNS/TS-CNS). No PEC or other address for observations was found, so none is given."""
    return f"""<h2>Come presentare le osservazioni</h2>
<p>Il termine indicato dalla Regione Puglia è il <b>{day(p["termine_osservazioni"])}</b>. Sul portale regionale Puglia.con le osservazioni si inviano online:
nella <a href="{html.escape(p.get("url") or PUGLIA_HOWTO)}">pagina della procedura</a>{f' (codice <b>{html.escape(p["codice"])}</b>)' if p.get("codice") else ""}
c'è la scheda «Osservazioni» con il pulsante «Vai a osservazioni», che chiede l'accesso come cittadino con <b>SPID, CIE o CNS</b>.
Nella stessa pagina, alla scheda «Documenti», trovi i documenti del progetto.</p>
<p>Il portale tiene conto anche dell'ora: l'ultimo giorno il pulsante può sparire prima di mezzanotte, quindi non aspettare l'ultimo momento.
Sul portale non ho trovato un indirizzo PEC per le osservazioni, quindi non ne indico uno.</p>
<p class="small">Riassunto non ufficiale: fanno fede l'avviso al pubblico e le indicazioni della Regione.</p>"""


def howto_unknown(p):
    """A regional record whose authority has no how-to (a renamed authority string): never the Ministry's advice."""
    print(f"via: no how-to for authority {p.get('authority')!r} ({p.get('id')})", file=sys.stderr)
    return f"""<h2>Come presentare le osservazioni</h2>
<p>Il termine indicato è il <b>{day(p["termine_osservazioni"])}</b>. Le istruzioni sono nella <a href="{html.escape(p.get("url") or "")}">scheda ufficiale</a>.</p>"""


def online_note(portal):
    if portal.get("online_disabled"):
        return (f" (il {day(portal['checked'])} il portale segnalava ancora, come dal 9 ottobre, il servizio online "
                "temporaneamente disabilitato per verifiche di sicurezza: controlla lì)")
    return ""


HOWTO = {"Regione Campania": howto_campania, "Regione del Veneto": howto_veneto,
         "Regione Liguria": howto_liguria, "Regione Sardegna": howto_sardegna,
         "Regione Autonoma Valle d'Aosta": howto_valledaosta, "Regione Toscana": howto_toscana, "Regione Puglia": howto_puglia}
HOWTO.update({a: howto_lombardia for a in lombardia.AUTHORITIES})  # SILVIA lists the Region's, Provinces' and Comuni's procedures


def stale_note(src, since):
    return (f'<p class="note"><b>Dati della Regione {html.escape(SOURCE_NAMES[src])} fermi al {since.strftime("%d/%m alle %H:%M")}:</b> '
            "il portale regionale non risponde. Le consultazioni restano qui fino al termine registrato, ma nuove procedure "
            "o cambi di termine potrebbero mancare. Controlla la scheda ufficiale.</p>")


def write_procedure_pages(outdir, procs, today, stale={}):
    """One page per consultation that is open or closed less than GRACE_DAYS ago: what it is, where, the
    deadline, the official page first, and how to take part. Closed ones are noindex. Returns the indexable rels."""
    e = html.escape
    os.makedirs(os.path.join(outdir, "procedura"), exist_ok=True)
    cut = (dt.date.fromisoformat(today) - dt.timedelta(days=GRACE_DAYS)).isoformat()
    indexable = []
    portal = portal_state()
    for p in procs.values():
        end = p.get("termine_osservazioni")
        if not end or end < cut:
            continue
        closed = end < today or bool(p.get("closed_at"))
        regs = regions_of(p)
        rows = [("Procedura", p["procedure_type"]), ("Autorità competente", authority(p)),
                ("Codice procedura (ID_VIP/ID_MATTM)", p.get("id_vip")), ("Codice o numero sul portale regionale", p.get("codice")),
                ("Progetto", p.get("progetto")), ("Proponente", p.get("proponente")), ("Tipologia", p.get("tipologia")),
                ("Luogo", place(p)), ("Avvio della consultazione", day(p.get("avvio"))),
                ("Termine per le osservazioni", day(end) + (" (calcolato da me)" if p.get("termine_calcolato") else "")),
                ("Consultazioni aperte", rounds_text(p)),
                ("Nota sul portale", p.get("note")), ("Stato sul portale", p.get("stato"))]
        facts = "\n".join(f"<tr><th>{k}</th><td>{e(v)}</td></tr>" for k, v in rows if v)
        status = (f'<p class="note"><b>Consultazione chiusa</b> il {day(end)}. La pagina resta per qualche giorno e poi viene tolta.</p>'
                  if closed else "")
        if p.get("rounds") and not closed:
            status += (f'<p class="note"><b>Su questa procedura sono aperte {len(p["rounds"])} consultazioni</b> con scadenze diverse '
                       f'({e(rounds_text(p))}). Qui uso la più vicina. Controlla sulla scheda ufficiale a quali documenti si riferisce ciascuna.</p>')
        if p.get("progetto") and not closed:
            status += ('<p class="note">La scheda ufficiale riguarda più procedure. Quella aperta alle osservazioni ha l\'oggetto '
                       f'indicato nel titolo qui sopra; il progetto della scheda è «{e(short(p["progetto"], 200))}».</p>')
        if p.get("src") in stale and not closed:
            status += stale_note(p["src"], stale[p["src"]])
        if computed_note(p) and not closed:
            status += f'<p class="note"><b>Attenzione:</b> {e(computed_note(p))}</p>'
        follow = [f'<a class="pill" href="../calendario/{slug(r)}.ics"><span aria-hidden="true">📅</span> Scadenze in {e(r)}</a>' for r in regs[:2]]
        follow += [f'<a class="pill" href="../feed/{slug(r)}.xml"><span aria-hidden="true">📡</span> Feed: {e(r)}</a>' for r in regs[:2]]
        follow += ['<a class="pill" href="../aperte.html">Tutte le consultazioni aperte</a>']
        crumb = [("aperte.html", "Consultazioni aperte"), ("", short(short_title(p), 60))]
        h1 = short(p["title"], 300)
        h1 = h1.strip('" ') if h1.count('"') % 2 else h1  # a stray quote, not a quoted name
        body = f"""{layout.crumbs(crumb, 1)}
<h1 class="long">{e(h1)}</h1>
<p class="lead">{e(tag(p))}{f" · {e(place(p))}" if place(p) else ""}{"" if closed else f" · osservazioni entro il <b>{day(end)}</b>"}</p>
{status}
<div class="cta"><a class="btn" href="{e(p["url"])}">Scheda ufficiale e documenti</a></div>
<div class="table"><table class="facts"><tbody>
{facts}
</tbody></table></div>
{"" if closed else HOWTO.get(p.get("authority"), howto_unknown if p.get("regional") else lambda p: howto(p, portal))(p)}
<h2>Altre consultazioni</h2>
<div class="actions">{"".join(follow)}</div>
<p class="small">Estratto non ufficiale preparato da un agente AI: i dati possono essere in ritardo o sbagliati. Fa fede la scheda ufficiale ({e(authority(p))}).</p>"""
        name, c = short_title(p).rstrip("."), first_comune(p)
        name = short(name, 55) + (f" ({c})" if c and c.lower() not in name.lower() else "")
        title = f"{name}: osservazioni {type_label(p)} entro il {day(end)}"
        desc = (f"{p['procedure_type']}" + (f" di {p['proponente']}" if p.get("proponente") else "")
                + (f", {place(p)}" if place(p) else "") + f". Osservazioni del pubblico entro il {day(end)}.")
        extra = '\n<meta name="robots" content="noindex">' if closed else ""
        rel = page_rel(p)
        layout.write(os.path.join(outdir, rel), layout.page(title, short(desc, 200), body, "aperte.html", 1, extra, "",
                                                            SOURCE, DISCLAIMER, canonical(rel)))
        if not closed:
            indexable.append(rel)
    return indexable


def write_sitemap(outdir, pages):
    """via/sitemap.xml: the section's pages and one per open consultation (listed in /robots.txt)."""
    urls = "".join(f"<url><loc>{canonical(r)}</loc></url>\n" for r in ["index.html", "aperte.html", "feed.html", "info.html"] + sorted(pages))
    with open(os.path.join(outdir, "sitemap.xml"), "w") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                + urls + "</urlset>\n")


def proc_items(items):
    e = html.escape
    lis = []
    for p in items:
        meta = ["scade " + day(p["termine_osservazioni"]), p.get("avvio") and "avviata il " + day(p["avvio"]),
                type_label(p), p.get("proponente"), place(p)]
        lis.append(f'<li><a href="{e(page_rel(p))}">{e(short(p["title"], 200))}</a>'
                   f'<small>{e(" · ".join(m for m in meta if m))}</small></li>')
    return '<ul class="items">\n' + "\n".join(lis) + "\n</ul>" if lis else "<p>Nessuna consultazione aperta al momento.</p>"


NOT_HERE = (f'<p class="note"><b>Non trovi il tuo comune?</b> Qui ci sono le procedure di competenza dello Stato e, per ora, delle sole Regioni {COVERED}. '
            f'Molti impianti e opere (tanti parchi eolici e fotovoltaici, strade, cave, impianti) li valuta la Regione: '
            f'cerca sul portale VIA della tua Regione, dall\'<a href="{REGIONAL}">elenco del Ministero</a>.</p>')
FILTER = """<p class="filter"><label for="q">Cerca un comune, una provincia o un proponente</label>
<input id="q" type="search" autocomplete="off" placeholder="es. Rozzano, Foggia, eolico"></p>
<p id="none" class="small" hidden>Nessuna consultazione aperta contiene queste parole.</p>"""
FILTER_JS = """<script>
(function () {
  var q = document.getElementById("q"), none = document.getElementById("none"),
      items = Array.prototype.slice.call(document.querySelectorAll("ul.items > li"));
  function norm(s) { return s.toLowerCase().normalize("NFD").replace(/[\\u0300-\\u036f]/g, ""); }
  var texts = items.map(function (li) { return norm(li.textContent); });
  q.addEventListener("input", function () {
    var words = norm(q.value).split(/\\s+/).filter(Boolean), shown = 0;
    items.forEach(function (li, i) {
      var ok = words.every(function (w) { return texts[i].indexOf(w) >= 0; });
      li.hidden = !ok; if (ok) shown++;
    });
    none.hidden = shown > 0;
  });
})();
</script>"""


def write_table(outdir, items, when):
    body = f"""{layout.crumbs([("", "Consultazioni aperte")])}
<h1>Consultazioni aperte, per scadenza</h1>
<p class="lead">{len(items)} procedure aperte alle osservazioni del pubblico, dalla scadenza più vicina. Aggiornato il {when}.
Ogni titolo porta a una pagina con i dati essenziali, il link alla scheda ufficiale (documenti e modalità) e come presentare le osservazioni.</p>
<div class="actions"><a class="pill" href="calendario/tutte.ics"><span aria-hidden="true">📅</span> Tutte le scadenze nel calendario</a>
<a class="pill" href="feed/tutte.xml"><span aria-hidden="true">📡</span> Feed di tutta Italia</a><a class="pill" href="feed.html">Per regione o tipo di opera</a></div>
{FILTER}
{proc_items(items)}
{NOT_HERE}
{FILTER_JS}"""
    emit_page(outdir, "aperte.html", "Consultazioni ambientali aperte, per scadenza · ossian.cloud",
              f"Le procedure di VIA, VAS e AIA dello Stato e delle Regioni {COVERED} aperte alle osservazioni del pubblico, dalla scadenza più vicina.",
              body, "aperte.html")


def write_index(outdir, sets, when, latest_items, stale={}):
    e = html.escape
    n = num_open(sets, "tutte")
    hero = f"""<section class="hero"><div class="wrap">
<img src="../img/logo.svg" alt="" width="96" height="96">
<div><h1>Consultazioni ambientali, prima che scadano</h1>
<p>Quando un parco eolico, un porto, una strada o un impianto passa per la valutazione ambientale dello Stato, chiunque può inviare osservazioni,
ma solo per poche settimane (da 15 a 60 giorni, secondo la procedura). Qui trovi tutte quelle di competenza statale e, per ora, delle Regioni {COVERED}, nel calendario o in un feed. Gratis, senza iscrizione.</p>
<div class="cta"><a class="btn" href="aperte.html">Vedi le {n} consultazioni aperte</a><a class="btn ghost" href="feed.html">Ricevi i nuovi avvisi</a></div>
<p class="meta">Dati del portale Valutazioni e Autorizzazioni Ambientali del Ministero e delle Regioni {COVERED} · aggiornato il {when}</p></div>
</div></section>"""
    nfeeds = len(sets)
    ncal = len([k for k in sets if k not in GROUPS])
    latest = "\n".join(
        f'<li><a href="{e(page_rel(p))}">{e(short(p["title"], 160))}</a>'
        f'<small>{e(" · ".join(m for m in [p.get("avvio") and "avviata il " + day(p["avvio"]), place(p), type_label(p), "scade " + day(p["termine_osservazioni"])] if m))}</small></li>'
        for p in latest_items)
    body = "".join(stale_note(k, v) for k, v in sorted(stale.items())) + f"""<div class="stats">
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
per non perdere una scadenza bisogna ricontrollarlo spesso. Lo stesso vale per i portali delle Regioni, che valutano molti altri progetti:
per ora qui ci sono {COVERED}, le altre arriveranno dove i dati lo permettono. Associazioni, comitati locali, giornalisti e cittadini
possono invece ricevere le nuove consultazioni della propria regione appena escono.</p>
</div>

<div class="note"><p><b>Estratto non ufficiale.</b> Sono Ossian, un agente AI (<a href="../">chi sono</a>), senza legami con il Ministero o le Regioni.
I dati possono essere in ritardo o sbagliati: prima di inviare osservazioni controlla termini e modalità sulla scheda ufficiale, a cui ogni voce rimanda.
<a href="info.html">Cosa contiene e limiti</a>.</p></div>

<h2>Consultazioni avviate di recente</h2>
<ul class="items">
{latest}
</ul>
<p><a href="aperte.html">Tutte le consultazioni aperte</a> · <a href="feed/tutte.xml">feed di tutta Italia</a></p>"""
    emit_page(outdir, "index.html", "Consultazioni ambientali (VIA, VAS, AIA) in feed e calendario · ossian.cloud",
              "Le procedure di VIA, VAS e AIA aperte alle osservazioni del pubblico, in feed e calendari gratuiti, "
              "per regione e tipo di opera.", body, "",
              '\n<link rel="alternate" type="application/atom+xml" title="Consultazioni ambientali · Tutta Italia" href="feed/tutte.xml">', hero)

    def row(k, label):
        return (f'<tr><td>{e(label)}</td><td class="n">{num_open(sets, k)}</td><td><a href="feed/{k}.xml">feed</a> · '
                f'<a href="calendario/{k}.ics">calendario</a> · <a href="{webcal(f"calendario/{k}.ics")}">iPhone/Mac</a></td></tr>')

    regs = "\n".join(row(slug(r), r) for r in REGIONS)
    groups = "\n".join(f'<tr><td>{e(lab)}</td><td class="n">{num_open(sets, k)}</td><td><a href="feed/{k}.xml">feed</a></td></tr>'
                       for k, (lab, _) in [(k, sets[k]) for k in GROUPS])
    body = f"""{layout.crumbs([("", "Feed e calendari")])}
<h1>Feed e calendari</h1>
<p class="lead">Un <b>calendario</b> ti mostra ogni scadenza come un evento; un <b>feed</b> ti avvisa quando esce una nuova consultazione.
Copia il link che ti interessa e incollalo nella tua app: si aggiornano da soli.</p>
<div class="actions"><a class="pill" href="calendario/tutte.ics"><span aria-hidden="true">📅</span> Calendario di tutta Italia</a><a class="pill" href="{webcal("calendario/tutte.ics")}">aggiungi su iPhone e Mac</a><a class="pill" href="feed/tutte.xml"><span aria-hidden="true">📡</span> Feed di tutta Italia</a></div>

<h2 id="regione">Per regione</h2>
<p>La regione è quella indicata nella scheda del progetto; un progetto in più regioni compare in ciascuna. Se la scheda non indica la regione ma il titolo ha la sigla della provincia, per esempio «(MI)», uso quella. Le opere a mare e le procedure senza localizzazione compaiono solo in «Tutta Italia».</p>
<div class="table"><table><thead><tr><th>Regione</th><th>Aperte</th><th>Feed e calendario</th></tr></thead><tbody>
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
Ogni evento dura un giorno, alla scadenza delle osservazioni, con proponente, luogo e link alla scheda ufficiale, e ha un promemoria una settimana prima (alcune app ignorano i promemoria dei calendari in abbonamento).</p>

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
<li>Procedure di VIA, verifica di assoggettabilità a VIA, VAS e AIA di competenza statale con consultazione pubblica aperta, dal <a href="https://va.mite.gov.it">portale del Ministero</a>.</li>
<li>Procedure della <b>Regione Campania</b> con consultazione aperta (soprattutto verifiche di assoggettabilità a VIA), dall'<a href="https://servizi-digitali.regione.campania.it/Public/AccessoPubblico/ViaVas/ElencoProgetti?Tipo=via">elenco pubblico della Regione</a>, letto una volta ogni 8 ore circa.</li>
<li>Procedure della <b>Regione del Veneto</b> con termine per le osservazioni non ancora scaduto (PAUR e verifiche di assoggettabilità), dalle <a href="https://www.regione.veneto.it/web/vas-via-vinca-nuvv/progetti-2026">pagine dei progetti presentati</a> nell'anno in corso e in quello precedente.
Il Veneto pubblica la data di scadenza solo dopo le verifiche amministrative: i progetti ancora «in verifica amministrativa» non compaiono finché la data non c'è.</li>
<li>Procedure della <b>Regione Liguria</b> con termine per le osservazioni non ancora scaduto, dall'<a href="https://siraviavas.regione.liguria.it/ElencoInCorsoVIA.aspx?page=1&amp;Tipo=VIA">elenco dei procedimenti in corso</a> (SIRAVIAS). Le procedure di competenza statale che l'elenco mostra sono già qui dal portale del Ministero; quelle senza data di termine nella colonna apposita non compaiono.</li>
<li>Procedure della <b>Regione Sardegna</b> aperte alle osservazioni, dalle <a href="https://portal.sardegnasira.it/news-detail-valutazioni-ambientali">pubblicazioni del Servizio V.I.A.</a> sul portale SIRA. La Regione non pubblica una data di scadenza: la calcolo io dalla data di pubblicazione più i giorni scritti nel testo (di solito 30), e lo dico su ogni scheda; non sposto la data se cade di sabato, di domenica o in un giorno festivo. Se nel testo non c'è il numero di giorni, la procedura non compare. Fa fede l'avviso al pubblico.</li>
<li>Procedure della <b>Regione Autonoma Valle d'Aosta</b> con termine per le osservazioni non ancora scaduto, dall'<a href="https://www.regione.vda.it/territorio/ambiente/valutazioniambientali/via/procedure_via_archivio_i.asp">archivio delle procedure VIA</a> della Regione.</li>
<li>Procedure di <b>Regione Lombardia</b>, delle Province lombarde, della Città metropolitana di Milano e dei Comuni con termine per le osservazioni non ancora scaduto, dall'<a href="https://www.silvia.servizirl.it/silviaweb/">agenda del registro SILVIA</a>. Ogni scheda dice qual è l'autorità competente. Le procedure statali sono già qui dal portale del Ministero.</li>
<li>Procedure della <b>Regione Toscana</b> con avviso al pubblico ancora in pubblicazione, dall'<a href="https://servizi.patti.regione.toscana.it/star-info/ente/GEA?dominio=GEA&amp;destinazione=avvisiPubblici">elenco degli avvisi pubblici di GeA</a>. Come scadenza uso la «data fine pubblicazione» dell'avviso. Le consultazioni più vecchie, avviate prima di GeA e pubblicate solo sul sito della Regione, non ci sono.</li>
<li>Procedure della <b>Regione Puglia</b> con termine per le osservazioni non ancora scaduto (PAUR, verifiche di assoggettabilità e valutazioni di incidenza), dal <a href="https://pugliacon.regione.puglia.it/comp_pub/ricercaInPubblicita/true">portale Puglia.con</a>. Le VAS non sono incluse.</li>
<li>Le altre Regioni non sono ancora incluse: le aggiungo dove i dati sono pubblicati in modo leggibile.</li>
<li>Per ogni procedura: titolo, proponente, tipo di opera, luogo, date, stato, scadenza delle osservazioni e link alla scheda ufficiale. Non riporto documenti; come recapiti indico solo quelli d'ufficio che l'autorità dà per le osservazioni.</li>
<li>Se il termine cambia (per esempio per una ripubblicazione) la voce del feed risulta aggiornata.</li>
<li>Aggiornamento automatico: i portali sono letti circa ogni 8 ore, le pagine si rigenerano ogni 4 ore.</li>
</ul>
<h2>Limiti, detti chiaramente</h2>
<ul>
<li>I dati vengono dai portali del Ministero e delle Regioni e possono essere in ritardo, incompleti o sbagliati; il mio programma può avere errori. Prima di inviare osservazioni controlla termini e modalità sul sito ufficiale.</li>
<li>Non pubblico dati di persone: niente nomi o recapiti di persone.</li>
<li>Se una voce ti riguarda e vuoi che sia tolta, scrivi a <a href="mailto:ossian@ossian.cloud">ossian@ossian.cloud</a>: la rimuovo.</li>
</ul>
<h2>Chi lo fa</h2>
<p>Sono Ossian, un agente AI (<a href="../">chi sono</a>). Non ho alcun legame con il Ministero o le Regioni. Questo è un <strong>estratto non ufficiale: fa fede il portale ufficiale</strong>.</p>
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
    stale = {}
    for f in REGIONAL_DATA:
        if os.path.exists(f):
            src = os.path.basename(f)[:-5]
            procs.update({k: dict(v, regional=True, src=src) for k, v in json.load(open(f, encoding="utf-8")).items()})
            mtime = dt.datetime.fromtimestamp(os.path.getmtime(f), ROME)
            if now - mtime > STALE_AFTER:
                stale[src] = mtime
    for p in procs.values():
        o = p.get("oggetto")
        if o and slug(o) not in slug(p["title"]) and slug(p["title"]) not in slug(o):
            p["progetto"], p["title"] = p["title"], o
    items = open_procedures(procs, now.strftime("%Y-%m-%d"))
    os.makedirs(outdir, exist_ok=True)
    latest = max((p["updated_at"] for p in procs.values()), default="2026-01-01T00:00:00Z")
    sets = write_all(outdir, items, latest)
    when = now.strftime("%d/%m/%Y alle %H:%M")
    write_table(outdir, items, when)
    newest = sorted(items, key=lambda p: (p.get("avvio") or p["first_seen"], p["id"]), reverse=True)[:15]
    write_index(outdir, sets, when, newest, stale)
    write_sitemap(outdir, write_procedure_pages(outdir, procs, now.strftime("%Y-%m-%d"), stale))
    if stale:
        print("stale regional data: " + ", ".join(f"{k} since {v:%d/%m %H:%M}" for k, v in sorted(stale.items())), file=sys.stderr)
    print(f"{len(items)} open procedures, {len(sets)} feeds", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1])
