# via-feed

Free Atom feeds and iCalendar (.ics) deadline calendars for Italian environmental assessment
procedures (VIA, verifica di assoggettabilità, AIA) that are open for public observations: the State's, and
so far one Region's (Campania).

Sources: Ministero dell'Ambiente e della Sicurezza Energetica, portale Valutazioni e Autorizzazioni
Ambientali (https://va.mite.gov.it); Regione Campania, public list of VIA procedures
(https://servizi-digitali.regione.campania.it/Public/AccessoPubblico/ViaVas/ElencoProgetti?Tipo=via).
Neither has a feed or alert for new consultations.

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
- `build.py OUTDIR`: writes `index.html`, an HTML table of open consultations, Atom feeds (all, per region, per type group)
  and .ics calendars (all, per region). Entry ids are stable (`tag:ossian.cloud,2026:via/<id>`); `updated` changes when the deadline changes.
- `atom.py`, `ics.py`: small stdlib writers.

## Limits

- Regional VIA/AIA systems are covered only for Campania so far.
- Parsing is by regular expressions on server-rendered HTML; a layout change breaks it (fetch refuses to write an empty result).
- A procedure with no region on the portal gets one from a province code in its title, e.g. "(MI)".
- Personal data is never stored: the "responsabile del procedimento" is dropped, as is any field matching an
  email, phone or fiscal-code pattern, and proponents that look like a natural person's name.
- Unofficial extract: the ministry portal is authoritative. Licence of the source data is ambiguous
  (portal footer "tutti i diritti riservati", ministry site CC BY 4.0); every item credits the source and links to it.

## Licence

Code: MIT (see LICENSE). Data belongs to its source.
