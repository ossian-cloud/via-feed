"""Minimal iCalendar (RFC 5545) writer for all-day deadline events (stdlib only)."""
import datetime as dt
import os


def _esc(s):
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r", "").replace("\n", "\\n")


def _fold(line):
    """Fold to 75 octets per line without splitting a UTF-8 character."""
    out, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > (75 if not out else 74):
            out.append(cur)
            cur = b""
        cur += b
    out.append(cur)
    return "\r\n ".join(x.decode("utf-8") for x in out)


def _stamp(iso):
    return iso[:19].replace("-", "").replace(":", "") + "Z"


def write_calendar(path, *, name, description, events):
    """events: dicts with uid, date (YYYY-MM-DD), stamp (ISO UTC), summary, description, url."""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//ossian.cloud//via-feed//IT",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             f"X-WR-CALNAME:{_esc(name)}", f"X-WR-CALDESC:{_esc(description)}",
             "X-WR-TIMEZONE:Europe/Rome", "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
             "X-PUBLISHED-TTL:PT12H"]
    for ev in events:
        day = dt.date.fromisoformat(ev["date"])
        lines += ["BEGIN:VEVENT", f"UID:{ev['uid']}", f"DTSTAMP:{_stamp(ev['stamp'])}",
                  f"DTSTART;VALUE=DATE:{day:%Y%m%d}",
                  f"DTEND;VALUE=DATE:{day + dt.timedelta(days=1):%Y%m%d}",
                  f"SUMMARY:{_esc(ev['summary'])}", f"DESCRIPTION:{_esc(ev['description'])}",
                  f"URL:{ev['url']}", "TRANSP:TRANSPARENT", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    with open(path + ".tmp", "w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(_fold(l) for l in lines) + "\r\n")
    os.replace(path + ".tmp", path)
