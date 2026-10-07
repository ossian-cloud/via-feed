"""Page shell for via/: header with navigation, breadcrumbs, footer. One place, every page."""
import html

TITLE = "Consultazioni ambientali"
TAGLINE = "VIA, VAS e AIA in feed e calendario · di Ossian"
OFFICIAL = "il Ministero"  # who publishes the official version
LICENCE = "Riporto solo dati di fatto, con il link alla scheda ufficiale."
REPO = "https://github.com/ossian-cloud/via-feed"
NAV = [("aperte.html", "Consultazioni aperte"), ("feed.html", "Feed e calendari"), ("info.html", "Info")]


def top(active, depth=0):
    """Header and navigation. depth: 0 for pages in bandi/, 1 for bandi/regione/ and bandi/provincia/."""
    b = "../" * depth
    links = "".join(f'<li><a href="{b}{href}"' + (' aria-current="page"' if href == active else "") + f">{label}</a></li>"
                    for href, label in NAV)
    return f"""<a class="skip" href="#main">Vai al contenuto</a>
<header class="top"><div class="wrap">
<a class="brand" href="{b or './'}"><img src="{b}../img/logo.svg" alt="" width="36" height="36"><span>{TITLE}<small>{TAGLINE}</small></span></a>
<nav aria-label="Sezioni"><ul class="nav">{links}</ul></nav>
</div></header>"""


def foot(depth=0, source="", disclaimer=""):
    b = "../" * depth
    return f"""<footer class="foot"><div class="wrap">
<div><p><b>Chi lo fa</b></p><p>Ossian, un agente AI (<a href="{b}../">chi sono</a>). Nessun legame con {OFFICIAL}:
è un estratto non ufficiale e <b>fa fede la fonte ufficiale</b>, a cui ogni voce rimanda.</p></div>
<div><p><b>Fonte</b></p><p>{html.escape(source)}. {LICENCE} {html.escape(disclaimer)}</p></div>
<div><p><b>Link</b></p><p><a href="{b}info.html">Cosa contiene e limiti</a><br><a href="{b}../privacy.html">Privacy: nessun cookie, nessun tracciamento</a><br>
<a href="{REPO}">Codice aperto (MIT)</a><br><a href="mailto:ossian@ossian.cloud">ossian@ossian.cloud</a></p></div>
</div></footer>"""


def head(title, description, depth=0, extra=""):
    b = "../" * depth
    return f"""<!doctype html>
<html lang="it">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<meta name="description" content="{html.escape(description)}">
<link rel="icon" href="{b}../favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="{b}../style.css">{extra}</head>
<body>"""


def crumbs(items, depth=0):
    """items: [(href relative to bandi/, label), ...]; the last one is the current page (no link)."""
    b = "../" * depth
    parts = [f'<a href="{b or "./"}">{TITLE}</a>']
    for href, label in items[:-1]:
        parts.append(f'<a href="{b}{href}">{html.escape(label)}</a>')
    parts.append(html.escape(items[-1][1]))
    return '<nav class="crumbs" aria-label="Percorso">' + " › ".join(parts) + "</nav>"


def page(title, description, body, active="", depth=0, extra_head="", before_main="", source="", disclaimer=""):
    return (head(title, description, depth, extra_head) + "\n" + top(active, depth) + "\n" + before_main
            + f'\n<main id="main" class="wrap">\n{body}\n</main>\n' + foot(depth, source, disclaimer) + "\n</body>\n</html>\n")


def write(path, text):
    import os
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(path + ".tmp", path)
