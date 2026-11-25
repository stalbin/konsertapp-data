#!/usr/bin/env python3
"""
Hämtar kommande konserter (från och med dagens datum) från:
  - Laholms konsertförening   (WordPress + The Events Calendar, JSON-API)
  - Varbergs konsertförening  (WordPress, generalprogram-sidan)
  - Falkenbergs konsertförening (statisk HTML, detaljsidor för klockslag)
  - Halmstads konsertförening (Webnode, fritext)

Resultatet skrivs ut som en tabell och sparas i konserter.json.

Krav:  pip install requests beautifulsoup4
Kör:   python konserter.py            (vanlig körning)
       python konserter.py --debug    (visar mer om vad som tolkas)
"""

from __future__ import annotations

import html
import json
import re
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import date, datetime, timedelta
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

TZ = ZoneInfo("Europe/Stockholm")
TODAY = datetime.now(TZ).date()
DEBUG = "--debug" in sys.argv

HEADERS = {"User-Agent": "Konsertapp/0.1 (hobbyprojekt; samlar konsertprogram i Halland)"}
PAUSE = 1.0  # sekunder mellan anrop, för att vara snäll mot små sajter

MONTHS = {
    "jan": 1, "januari": 1, "feb": 2, "februari": 2, "mar": 3, "mars": 3,
    "apr": 4, "april": 4, "maj": 5, "jun": 6, "juni": 6, "jul": 7, "juli": 7,
    "aug": 8, "augusti": 8, "sep": 9, "sept": 9, "september": 9,
    "okt": 10, "oktober": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
DATE_RE = re.compile(
    r"\b(\d{1,2})\s*(?:e|:e)?\s+(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\b\.?",
    re.IGNORECASE,
)
YEAR_RE = re.compile(r"\b(20\d{2})\b")
TIME_RE = re.compile(r"\bkl\.?\s*(\d{1,2})[.:](\d{2})\b", re.IGNORECASE)
TIME_LOOSE_RE = re.compile(r"\b([01]?\d|2[0-3])[.:]([0-5]\d)\b")


@dataclass
class Konsert:
    forening: str
    titel: str
    datum: str            # ÅÅÅÅ-MM-DD
    tid: str | None       # HH:MM eller None
    lokal: str | None
    pris: str | None = None
    lank: str | None = None
    festival: str | None = None
    extra: dict = field(default_factory=dict)


# ---------- hjälpfunktioner ----------

def log(*a):
    if DEBUG:
        print("  [debug]", *a, file=sys.stderr)


def fetch(url: str) -> requests.Response:
    log("hämtar", url)
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    time.sleep(PAUSE)
    return r


def soup_of(url: str) -> BeautifulSoup:
    r = fetch(url)
    r.encoding = r.apparent_encoding or r.encoding
    return BeautifulSoup(r.text, "html.parser")


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(s or "")).strip()


def text_lines(node) -> list[str]:
    return [clean(t) for t in node.get_text("\n").split("\n") if clean(t)]


def parse_date(text: str) -> date | None:
    """Tolkar svenska datum som '12 september 2026', 'tis 8 dec kl 15:00 2026'."""
    m = DATE_RE.search(text)
    if not m:
        return None
    day, month = int(m.group(1)), MONTHS[m.group(2).lower()]
    # Året kan stå direkt efter månaden eller lite senare (Halmstad: '... kl 15:00 2026')
    y = YEAR_RE.search(text[m.end(): m.end() + 30])
    if y:
        year = int(y.group(1))
    else:
        # Inget år: välj det år som gör datumet närmast framåt i tiden
        year = TODAY.year
        try:
            if date(year, month, day) < TODAY - timedelta(days=120):
                year += 1
        except ValueError:
            return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_time(text: str, loose: bool = False) -> str | None:
    m = TIME_RE.search(text) or (TIME_LOOSE_RE.search(text) if loose else None)
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


def festival_of(text: str) -> str | None:
    return "Halländsk musikfestival" if re.search(r"halländsk\s+musikfestival", text, re.I) else None


def card_for(link_tag, date_required=True):
    """Klättrar uppåt från en länk tills vi når ett element som ser ut som ett konsertkort
    (innehåller ett datum men bara en konsertlänk)."""
    node = link_tag
    for _ in range(8):
        node = node.parent
        if node is None:
            return None
        txt = node.get_text(" ")
        links = {a.get("href") for a in node.find_all("a", href=True)}
        if len(links) > 3:
            return None
        if not date_required or DATE_RE.search(txt):
            return node
    return None


# ---------- Laholm ----------

def laholm() -> list[Konsert]:
    url = "https://www.laholmskonsertforening.se/wp-json/tribe/events/v1/events"
    params = f"?start_date={TODAY.isoformat()}&per_page=50"
    out, next_url = [], url + params
    while next_url:
        data = fetch(next_url).json()
        for e in data.get("events", []):
            start = datetime.strptime(e["start_date"], "%Y-%m-%d %H:%M:%S")
            venue = e.get("venue") or {}
            out.append(Konsert(
                forening="Laholm",
                titel=clean(e.get("title", "")),
                datum=start.date().isoformat(),
                tid=None if e.get("all_day") else start.strftime("%H:%M"),
                lokal=clean(venue.get("venue", "")) or None if isinstance(venue, dict) else None,
                pris=clean(e.get("cost", "")) or None,
                lank=e.get("url"),
                festival=festival_of(e.get("description", "")),
            ))
        next_url = data.get("next_rest_url")
    return out


# ---------- Varberg ----------

def varberg() -> list[Konsert]:
    base = "https://varbergskonsertforening.se"
    soup = soup_of(base + "/generalprogram/")
    out, seen = [], set()
    for a in soup.find_all("a", href=re.compile(r"/program/[^/]+/?$")):
        href = urljoin(base, a["href"])
        if href in seen:
            continue
        card = card_for(a)
        if card is None:
            continue
        seen.add(href)
        lines = text_lines(card)
        whole = " ".join(lines)
        d = parse_date(whole)
        if not d:
            continue
        # Titel: rubrik i kortet, annars länktexten
        h = card.find(re.compile(r"^h[1-6]$"))
        titel = clean(h.get_text(" ")) if h else clean(a.get_text(" "))
        # Lokal: den rad som inte är titel, datum, tid eller "Läs mer"
        lokal = None
        for ln in lines:
            if ln == titel or DATE_RE.search(ln) or TIME_RE.search(ln) or ln.lower().startswith("läs mer"):
                continue
            if re.fullmatch(r"(mån|tis|ons|tors|fre|lör|sön)\w*\.?", ln, re.I):
                continue
            lokal = ln
            break
        log("varberg kort:", lines)
        out.append(Konsert("Varberg", titel, d.isoformat(), parse_time(whole), lokal,
                           lank=href, festival=festival_of(whole)))
    if not out:
        log("varberg: inga kort med länkar, provar API + sidtext")
        out = varberg_fallback(base, soup)
    return out


def norm(s: str) -> str:
    return re.sub(r"[^a-zåäö0-9]", "", clean(s).lower())


VENUE_RE = re.compile(r"teater|sal\b|salen|arena|kyrka|hall|skola|aula|konserthus", re.I)
WEEKDAY_RE = re.compile(r"^(mån|tis|ons|tors|fre|lör|sön)\w*\.?$", re.I)


def varberg_fallback(base: str, soup: BeautifulSoup) -> list[Konsert]:
    """Titlar och länkar från WordPress-API:t, datum/tid/lokal från generalprogrammets text."""
    items = fetch(base + "/wp-json/wp/v2/program?per_page=100").json()
    lines = text_lines(soup.body or soup)
    nlines = [norm(l) for l in lines]

    # Hitta raden där varje titel står på sidan
    hits = []
    for it in items:
        titel = clean(it["title"]["rendered"])
        key = norm(titel)[:25]
        idx = next((i for i, nl in enumerate(nlines) if key and key in nl), None)
        if idx is None:
            log("varberg: hittade inte titeln på sidan:", titel)
            continue
        hits.append((idx, titel, it.get("link"), it.get("content", {}).get("rendered", "")))
    hits.sort()
    if not hits:
        return []

    # Står datumet före eller efter titeln? Kolla raderna efter sista titeln.
    last = hits[-1][0]
    after = any(DATE_RE.search(l) for l in lines[last + 1:last + 6])

    out = []
    for n, (idx, titel, link, content) in enumerate(hits):
        if after:
            end = hits[n + 1][0] if n + 1 < len(hits) else idx + 6
            seg = lines[idx + 1:end]
        else:
            start = hits[n - 1][0] + 1 if n > 0 else max(0, idx - 6)
            seg = lines[start:idx]
        seg_text = " ".join(seg)
        d = parse_date(seg_text)
        if not d:
            log("varberg: inget datum för", titel, seg)
            continue
        lokal = next((l for l in seg if VENUE_RE.search(l) and not DATE_RE.search(l)), None)
        if not lokal:
            lokal = next((l for l in seg if not (DATE_RE.search(l) or TIME_RE.search(l)
                          or WEEKDAY_RE.match(l) or l.lower().startswith("läs mer"))), None)
        out.append(Konsert("Varberg", titel, d.isoformat(), parse_time(seg_text), lokal,
                           lank=link, festival=festival_of(titel + " " + content)))
    return out


# ---------- Falkenberg ----------

def falkenberg() -> list[Konsert]:
    base = "https://www.falkenbergskonsertforening.se/"
    soup = soup_of(base)
    # Hittar säsongsmappen automatiskt, t.ex. program-26-27/6/6.html
    pat = re.compile(r"program-\d{2}-\d{2}/\d+/\d+\.html$")
    out, seen = [], set()
    for a in soup.find_all("a", href=pat):
        href = urljoin(base, a["href"])
        if href in seen:
            continue
        seen.add(href)
        card = card_for(a, date_required=False)
        card_lines = text_lines(card) if card else text_lines(a)
        card_text = " ".join(card_lines)
        d = parse_date(card_text)
        # Titel från kortet: allt utom datumraden
        titel = " – ".join(ln for ln in card_lines if not DATE_RE.search(ln)) or None

        # Detaljsidan har klockslag och lokal
        tid, lokal = None, None
        try:
            det = soup_of(href)
            det_lines = text_lines(det)
            det_text = "\n".join(det_lines)
            d = d or parse_date(det_text)
            # Leta klockslag nära datumraden först, sedan var som helst
            for i, ln in enumerate(det_lines):
                if DATE_RE.search(ln):
                    tid = parse_time(" ".join(det_lines[i:i + 3]))
                    if tid:
                        break
            tid = tid or parse_time(det_text)
            m = re.search(r"Argus[^\n]{0,40}", det_text)
            lokal = clean(m.group(0)) if m else None
            if not titel:
                h = det.find(re.compile(r"^h[1-3]$"))
                titel = clean(h.get_text(" ")) if h else None
            festival = festival_of(card_text + det_text)
        except requests.RequestException as e:
            log("kunde inte hämta detaljsida", href, e)
            festival = festival_of(card_text)

        if not d:
            log("inget datum för", href)
            continue
        out.append(Konsert("Falkenberg", titel or "(okänd titel)", d.isoformat(), tid,
                           lokal or "Argus, Falkenberg", lank=href, festival=festival))
    return out


# ---------- Halmstad ----------

def halmstad() -> list[Konsert]:
    base = "https://www.halmstadskonsertforening.se/"
    # Startsidan + sidan som menyn kallar "Kommande konserter" (just nu /ekonomi/)
    pages = [base, urljoin(base, "ekonomi/")]
    out, seen = [], set()
    for url in pages:
        try:
            soup = soup_of(url)
        except requests.RequestException as e:
            log("halmstad:", url, e)
            continue
        nortic = next((a["href"] for a in soup.find_all("a", href=True) if "nortic.se" in a["href"]), None)
        lines = text_lines(soup.body or soup)
        for i, ln in enumerate(lines):
            # Datumrader ser ut som 'tis 8 dec kl 15:00 2026 ...'
            if not (DATE_RE.search(ln) and TIME_RE.search(ln)):
                continue
            d = parse_date(ln)
            if not d:
                continue
            tid = parse_time(ln)
            key = (d, tid)
            if key in seen:
                continue
            seen.add(key)
            # Lokal: text efter året på samma rad, eller nästa rad
            after = re.split(r"\b20\d{2}\b", ln, maxsplit=1)
            lokal = clean(after[1]).strip(",–- ") if len(after) > 1 else ""
            # Titel: närmaste rad efter datumraden som inte är lokal/datum
            titel = None
            for nxt in lines[i + 1:i + 5]:
                if nxt != lokal and not DATE_RE.search(nxt) and len(nxt) > 3:
                    titel = nxt
                    break
            if not lokal and i + 1 < len(lines) and re.search(r"biograf|kyrka|teater|sal|hus", lines[i + 1], re.I):
                lokal = lines[i + 1]
            log("halmstad rad:", lines[max(0, i - 1):i + 4])
            out.append(Konsert("Halmstad", titel or "(okänd titel)", d.isoformat(), tid,
                               lokal or None, lank=nortic or url))
    return out


# ---------- huvudprogram ----------

SOURCES = {"Laholm": laholm, "Varberg": varberg, "Falkenberg": falkenberg, "Halmstad": halmstad}


def main():
    alla: list[Konsert] = []
    for namn, fn in SOURCES.items():
        print(f"Hämtar {namn} ...", file=sys.stderr)
        try:
            res = fn()
            print(f"  {len(res)} konserter hittade", file=sys.stderr)
            alla.extend(res)
        except Exception as e:  # en trasig sajt ska inte stoppa de andra
            print(f"  FEL för {namn}: {e}", file=sys.stderr)

    kommande = sorted(
        (k for k in alla if date.fromisoformat(k.datum) >= TODAY),
        key=lambda k: (k.datum, k.tid or "99:99"),
    )

    print(f"\nKonserter från och med {TODAY.isoformat()}:\n")
    for k in kommande:
        print(f"{k.datum}  {k.tid or '--:--'}  {k.forening:<10}  {k.titel}  |  {k.lokal or '?'}")

    with open("konserter.json", "w", encoding="utf-8") as f:
        json.dump([asdict(k) for k in kommande], f, ensure_ascii=False, indent=2)
    print(f"\n{len(kommande)} konserter sparade i konserter.json", file=sys.stderr)


if __name__ == "__main__":
    main()
