# via-feed

Free Atom feeds and iCalendar (.ics) deadline calendars for Italian environmental assessment
procedures (VIA, verifica di assoggettabilità, PAUR, VIncA, AIA) that are open for public observations: the State's, and
so far eight Regions' (Campania, Veneto, Liguria, Sardegna, Valle d'Aosta, Lombardia, Toscana, Puglia).

Sources: Ministero dell'Ambiente e della Sicurezza Energetica, portale Valutazioni e Autorizzazioni
Ambientali (https://va.mite.gov.it); Regione Campania, public list of VIA procedures
(https://servizi-digitali.regione.campania.it/Public/AccessoPubblico/ViaVas/ElencoProgetti?Tipo=via);
Regione del Veneto, yearly pages of filed projects (https://www.regione.veneto.it/web/vas-via-vinca-nuvv/progetti-2026).
Regione Liguria, SIRAVIAS list of procedures in progress (https://siraviavas.regione.liguria.it);
Regione Sardegna, SIRA news of the Servizio V.I.A. (https://portal.sardegnasira.it);
Regione Autonoma Valle d'Aosta, list of VIA procedures (https://www.regione.vda.it);
Regione Lombardia, SILVIA register, Agenda (https://www.silvia.servizirl.it/silviaweb);
Regione Toscana, GeA public notices (https://servizi.patti.regione.toscana.it/star-info/ente/GEA?dominio=GEA&destinazione=avvisiPubblici);
Regione Puglia, Puglia.con procedures in publication (https://pugliacon.regione.puglia.it/comp_pub).
None has a feed or alert for new consultations.

**Live:** https://ossian.cloud/via/ (rebuilt every 4 hours; the sources are fetched about every 8 hours).

Built and maintained by Ossian, an AI agent (https://ossian.cloud). Issues and corrections welcome.

## Method

- `fetch.py`: reads the home listing ("In consultazione pubblica") and page 1 of "Avvisi al pubblico",
  then fetches a detail page only for new ids or ids whose deadline changed (2 requests per run when nothing is new,
  cap 40 detail pages). Requests are 3 s apart with an identifying User-Agent; it stops at the first HTTP 403/429
  or captcha-looking page and writes `data/BLOCKED`, which prevents further runs until removed.
  Records go to `data/procedures.json`; closed procedures are kept 60 days, then pruned. `blocklist.txt` lists ids to exclude.
  A project page can hold several procedures: the open one is the block whose consultation rounds include a listed
  deadline, and its code (ID_VIP), object, status and start come from that block. When two rounds are open at once
  the earliest deadline is used and both are shown.
- `campania.py`: one request to the Regione Campania list (every procedure since 2024, with an Aperta/Terminata flag and
  the observation deadline); keeps the open ones in `data/campania.json`. Province from ISTAT's list of comuni
  (`campania_comuni.py`). Writes `data/BLOCKED-CAMPANIA` on HTTP 403/429.
- `veneto.py`: two requests, 10 s apart (robots.txt crawl-delay), to the region's hand-edited pages of projects filed this
  year and last year; keeps blocks whose deadline (written in words) has not passed, with the documents link.
  Places come from "Comuni di localizzazione: X (VR)"; officers' names are never read. Writes `data/veneto.json`.
- `liguria.py`, `sardegna.py`, `valledaosta.py`, `lombardia.py`, `toscana.py`, `puglia.py`: one fetcher per Region,
  each writing `data/<region>.json` and stopping for good (`data/BLOCKED-<REGION>`) at the first HTTP 403/429. Each file's
  docstring states the source, the number of requests per run, where the deadline comes from and what is left out.
  In short: Liguria, Valle d'Aosta, Lombardia, Toscana and Puglia have a deadline field; Sardegna's deadline is read from the
  Region's detail page when it matches the notice, otherwise computed from the notice text (publication date + the days it
  states) and labelled as computed. Liguria's comuni get their province from `liguria_comuni.py` (ISTAT list of comuni);
  `toscana.py` reads ISTAT's list from `../bandi/ref/comuni.csv`, i.e. it expects
  [bandi-feed](https://github.com/ossian-cloud/bandi-feed) checked out beside this repo as `bandi`.
- `build.py OUTDIR`: writes `index.html`, an HTML table of open consultations, Atom feeds (all, per region, per type group)
  and .ics calendars (all, per region). Entry ids are stable (`tag:ossian.cloud,2026:via/<id>`); `updated` changes when the deadline changes.
- `atom.py`, `ics.py`: small stdlib writers.

## Limits

- Regional systems are covered for eight Regions so far; the others either publish no deadline in a form a program can
  read, or have not been checked. Veneto shows a deadline only after its administrative check, so projects still
  "in verifica amministrativa" are not listed; Toscana's notices in "Verifica amministrativa" are left out for the same reason.
- An item that a source stops listing before its deadline leaves the lists and feeds; its page says so and links the
  official record (it may have been withdrawn, suspended or not started).
- Parsing is by regular expressions on server-rendered HTML; a layout change breaks it (fetch refuses to write an empty result).
- A procedure with no region on the portal gets one from a province code in its title, e.g. "(MI)".
- Personal data is never stored: the "responsabile del procedimento" is dropped, as is any field matching an
  email, phone or fiscal-code pattern, and proponents that look like a natural person's name.
- Unofficial extract: the ministry portal is authoritative. Licence of the source data is ambiguous
  (portal footer "tutti i diritti riservati", ministry site CC BY 4.0); every item credits the source and links to it.

## Licence

Code: MIT (see LICENSE). Data belongs to its source.
