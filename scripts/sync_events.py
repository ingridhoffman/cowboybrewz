#!/usr/bin/env python3
"""
Sync upcoming events from a Google Calendar iCal (.ics) feed into index.html.

Reads the calendar feed URL from the CALENDAR_ICS_URL environment variable,
selects upcoming events (those that haven't ended yet), formats each into a
brand-styled card, and replaces the content between the
<!-- EVENTS:START --> and <!-- EVENTS:END --> markers in index.html.

Designed to run in GitHub Actions on a schedule. Fails safe: if the feed
can't be fetched or parsed, it exits without modifying the page.

Usage:
    CALENDAR_ICS_URL="https://..." python scripts/sync_events.py
    python scripts/sync_events.py --ics sample.ics   # local testing
"""

import argparse
import datetime as dt
import html
import os
import re
import sys
import urllib.request
from zoneinfo import ZoneInfo

import icalendar
import recurring_ical_events

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_PATH = os.path.join(ROOT, "index.html")
START_MARKER = "<!-- EVENTS:START"
END_MARKER = "<!-- EVENTS:END -->"

# Events are shown/sorted in this timezone (Cowboy Brewz operates in WA).
LOCAL_TZ = ZoneInfo("America/Los_Angeles")

# Only show events starting within this many months from today.
MONTHS_AHEAD = 3

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def fetch_ics(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "cowboybrewz-sync/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def as_local_date(value) -> dt.date:
    """Normalize a DTSTART/DTEND value (date or datetime) to a local date."""
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=LOCAL_TZ)
        return value.astimezone(LOCAL_TZ).date()
    return value  # already a date (all-day event)


def add_months(d: dt.date, n: int) -> dt.date:
    """Return the date n months after d, clamping the day to the month's length."""
    month_index = d.month - 1 + n
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    # Clamp day (e.g. adding 1 month to Jan 31 -> Feb 28/29).
    if month == 12:
        next_month_first = dt.date(year + 1, 1, 1)
    else:
        next_month_first = dt.date(year, month + 1, 1)
    last_day = (next_month_first - dt.timedelta(days=1)).day
    return dt.date(year, month, min(d.day, last_day))


def clean_location(loc: str) -> str:
    """Trim Google's full postal addresses down to a tidy 'City, ST'.

    'Davenport, WA 99122, USA' -> 'Davenport, WA'. Venue names without a ZIP
    (e.g. 'Cowboy Brewz Taproom') are left untouched.
    """
    loc = loc.strip()
    # Drop a trailing country.
    loc = re.sub(r",?\s*(USA|United States(?: of America)?)\s*$", "", loc, flags=re.I)
    # Drop a trailing ZIP / ZIP+4.
    loc = re.sub(r"\s+\d{5}(-\d{4})?\s*$", "", loc)
    return loc.strip().rstrip(",").strip()


def format_date_range(start: dt.date, end_inclusive: dt.date) -> str:
    """Human-friendly date range, e.g. 'Aug 20–22, 2026' or 'Aug 30 – Sep 2, 2026'."""
    s, e = start, end_inclusive
    if s == e:
        return f"{MONTHS[s.month - 1]} {s.day}, {s.year}"
    if s.year == e.year and s.month == e.month:
        return f"{MONTHS[s.month - 1]} {s.day}–{e.day}, {s.year}"
    if s.year == e.year:
        return (f"{MONTHS[s.month - 1]} {s.day} – "
                f"{MONTHS[e.month - 1]} {e.day}, {s.year}")
    return (f"{MONTHS[s.month - 1]} {s.day}, {s.year} – "
            f"{MONTHS[e.month - 1]} {e.day}, {e.year}")


def parse_events(ics_bytes: bytes):
    """Return a sorted list of upcoming events: (start_date, end_date, name, location)."""
    cal = icalendar.Calendar.from_ical(ics_bytes)

    today = dt.datetime.now(LOCAL_TZ).date()
    cutoff = add_months(today, MONTHS_AHEAD)  # only show events starting on/before this
    window_start = dt.datetime.now(LOCAL_TZ)
    window_end = dt.datetime.combine(cutoff, dt.time.max, tzinfo=LOCAL_TZ)

    occurrences = recurring_ical_events.of(cal).between(window_start, window_end)

    events = []
    for comp in occurrences:
        start_raw = comp.get("DTSTART")
        end_raw = comp.get("DTEND")
        if start_raw is None:
            continue

        start_date = as_local_date(start_raw.dt)

        if end_raw is not None:
            end_val = end_raw.dt
            end_date = as_local_date(end_val)
            # For all-day events DTEND is exclusive (the morning after) — step back a day.
            if not isinstance(end_val, dt.datetime) and end_date > start_date:
                end_date = end_date - dt.timedelta(days=1)
            if end_date < start_date:
                end_date = start_date
        else:
            end_date = start_date

        # Skip events that have already finished or start beyond the cutoff.
        if end_date < today or start_date > cutoff:
            continue

        name = str(comp.get("SUMMARY", "")).strip()
        location = clean_location(str(comp.get("LOCATION", "")))
        if not name:
            continue

        events.append((start_date, end_date, name, location))

    # De-duplicate (recurring expansion can repeat) and sort by start date.
    seen = set()
    unique = []
    for ev in sorted(events, key=lambda e: (e[0], e[1], e[2])):
        key = (ev[0], ev[1], ev[2], ev[3])
        if key in seen:
            continue
        seen.add(key)
        unique.append(ev)
    return unique


def render_cards(events) -> str:
    indent = "      "
    if not events:
        return (f"{indent}<p class=\"events-empty\">New dates coming soon—"
                f"check back, y'all!</p>")

    cards = []
    for start_date, end_date, name, location in events:
        date_str = html.escape(format_date_range(start_date, end_date))
        name_str = html.escape(name)
        loc_html = ""
        if location:
            loc_html = f"\n{indent}  <span class=\"location\">{html.escape(location)}</span>"
        card = (
            f"{indent}<article class=\"event\">\n"
            f"{indent}  <span class=\"date\">{date_str}</span>\n"
            f"{indent}  <span class=\"name\">{name_str}</span>"
            f"{loc_html}\n"
            f"{indent}</article>"
        )
        cards.append(card)
    return "\n\n".join(cards)


def splice(html_text: str, cards: str) -> str:
    start = html_text.index(START_MARKER)
    start_line_end = html_text.index("\n", start) + 1  # keep the START marker line
    end = html_text.index(END_MARKER, start_line_end)
    end_line_start = html_text.rfind("\n", start_line_end, end) + 1  # keep END marker line
    return html_text[:start_line_end] + cards + "\n" + html_text[end_line_start:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ics", help="Path to a local .ics file (for testing).")
    args = ap.parse_args()

    try:
        if args.ics:
            with open(args.ics, "rb") as f:
                ics_bytes = f.read()
        else:
            url = os.environ.get("CALENDAR_ICS_URL", "").strip()
            if not url:
                print("CALENDAR_ICS_URL not set; nothing to sync.", file=sys.stderr)
                return 0
            ics_bytes = fetch_ics(url)

        events = parse_events(ics_bytes)
    except Exception as exc:  # noqa: BLE001 — fail safe, never break the page
        print(f"Sync failed, leaving page unchanged: {exc}", file=sys.stderr)
        return 1

    cards = render_cards(events)

    with open(INDEX_PATH, "r", encoding="utf-8") as f:
        original = f.read()

    updated = splice(original, cards)

    if updated == original:
        print(f"No changes ({len(events)} upcoming event(s)).")
        return 0

    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        f.write(updated)
    print(f"Updated index.html with {len(events)} upcoming event(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
