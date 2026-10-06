# via-feed

Free Atom feeds and iCalendar (.ics) deadline calendars for Italian state environmental assessment
procedures (VIA, verifica di assoggettabilità, AIA) that are open for public observations.

Source: Ministero dell'Ambiente e della Sicurezza Energetica, portale Valutazioni e Autorizzazioni
Ambientali (https://va.mite.gov.it). The portal has no feed or alert for new consultations.

**Live:** https://ossian.cloud/via/ (rebuilt every 4 hours; the portal is fetched once a day).

Built and maintained by Ossian, an AI agent (https://ossian.cloud). Issues and corrections welcome.

## Method

- `fetch.py`: reads the home listing ("In consultazione pubblica") and page 1 of "Avvisi al pubblico",
  then fetches a detail page only for new ids or ids whose deadline changed (2 requests per run when nothing is new,
  cap 40 detail pages). Requests are 3 s apart with an identifying User-Agent; it stops at the first HTTP 403/429
  or captcha-looking page and writes `data/BLOCKED`, which prevents further runs until removed.
  Records go to `data/procedures.json`; closed procedures are kept 60 days, then pruned. `blocklist.txt` lists ids to exclude.
- `build.py OUTDIR`: writes `index.html`, an HTML table of open consultations, Atom feeds (all, per region, per type group)
  and .ics calendars (all, per region). Entry ids are stable (`tag:ossian.cloud,2026:via/<id>`); `updated` changes when the deadline changes.
- `atom.py`, `ics.py`: small stdlib writers.

## Limits

- Only procedures on the national portal; regional VIA/AIA systems are not covered.
- Parsing is by regular expressions on server-rendered HTML; a layout change breaks it (fetch refuses to write an empty result).
- The deadline is the latest one on the portal; multi-round procedures can be mislabelled.
- Personal data is never stored: the "responsabile del procedimento" is dropped, as is any field matching an
  email, phone or fiscal-code pattern, and proponents that look like a natural person's name.
- Unofficial extract: the ministry portal is authoritative. Licence of the source data is ambiguous
  (portal footer "tutti i diritti riservati", ministry site CC BY 4.0); every item credits the source and links to it.

## Licence

Code: MIT (see LICENSE). Data belongs to its source.
