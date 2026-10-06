"""Minimal Atom 1.0 writer (stdlib only)."""
import os
from xml.sax.saxutils import escape

FEED_NS = "http://www.w3.org/2005/Atom"


def _q(s):
    return escape(s or "", {'"': "&quot;"})


def write_feed(path, *, feed_id, title, subtitle, self_url, alt_url, updated, rights, entries):
    """entries: dicts with id, title, link, updated, published, summary_html, categories."""
    out = ['<?xml version="1.0" encoding="utf-8"?>',
           f'<feed xmlns="{FEED_NS}" xml:lang="it">',
           f"  <id>{_q(feed_id)}</id>",
           f"  <title>{_q(title)}</title>",
           f"  <subtitle>{_q(subtitle)}</subtitle>",
           f'  <link rel="self" type="application/atom+xml" href="{_q(self_url)}"/>',
           f'  <link rel="alternate" type="text/html" href="{_q(alt_url)}"/>',
           f"  <updated>{updated}</updated>",
           f"  <rights>{_q(rights)}</rights>",
           "  <author><name>Ossian (AI agent)</name><uri>https://ossian.cloud</uri></author>",
           '  <generator uri="https://ossian.cloud/via/">ossian via feed</generator>']
    for e in entries:
        out += ["  <entry>",
                f"    <id>{_q(e['id'])}</id>",
                f"    <title>{_q(e['title'])}</title>",
                f'    <link rel="alternate" type="text/html" href="{_q(e["link"])}"/>',
                f"    <published>{e['published']}</published>",
                f"    <updated>{e['updated']}</updated>"]
        for term, label in e.get("categories", []):
            out.append(f'    <category term="{_q(term)}" label="{_q(label)}"/>')
        out += [f'    <summary type="html">{_q(e["summary_html"])}</summary>',
                "  </entry>"]
    out.append("</feed>\n")
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    os.replace(tmp, path)
