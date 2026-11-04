#!/usr/bin/env python3
"""
Avengers: Doomsday - BookMyShow Hyderabad ticket watcher.

Checks whether bookings are open and which cinemas have shows, then alerts you
by WhatsApp (CallMeBot, free) and a phone call (Twilio). Built to run on a
schedule in GitHub Actions, so it keeps working while your laptop is off.

Usage:
  python check_tickets.py                # normal check (sends alerts, saves state)
  python check_tickets.py --test-notify  # send a test WhatsApp + test call
  python check_tickets.py --dry-run      # check and print, send nothing, save nothing
"""
from __future__ import annotations

import argparse
import html
import json
import os
import random
import re
import sys
import time
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape as xml_escape

import requests

try:  # curl_cffi mimics a real Chrome browser, which helps avoid bot blocking
    from curl_cffi import requests as cffi_requests
except ImportError:  # falls back to plain requests
    cffi_requests = None


# --------------------------------------------------------------------------- #
# Configuration (all overridable with environment variables)
# --------------------------------------------------------------------------- #
def env(name: str, default: str = "") -> str:
    """GitHub passes '' for unset variables, so treat empty as unset."""
    value = (os.getenv(name) or "").strip()
    return value or default


def env_list(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in env(name, default).split(",") if x.strip()]


IST = timezone(timedelta(hours=5, minutes=30))
BASE = "https://in.bookmyshow.com"

CITY = env("BMS_CITY", "hyderabad")
REGION = env("BMS_REGION_CODE", "HYD")
SLUG = env("BMS_MOVIE_SLUG", "avengers-doomsday")
MOVIE_TITLE = env("MOVIE_TITLE", "Avengers: Doomsday")
EVENT_CODES = [c.upper() for c in env_list("BMS_EVENT_CODES")]
DATES_RAW = env_list("BMS_DATES", "20261217,20261218")
VENUE_FILTER = [v.lower() for v in env_list("VENUE_FILTER")]
ERROR_ALERT_AFTER = int(env("ERROR_ALERT_AFTER", "6"))
HEARTBEAT_HOUR_IST = env("HEARTBEAT_HOUR_IST", "21")  # set to "off" to disable
CALL_ON_POSSIBLE = env("CALL_ON_POSSIBLE", "false").lower() == "true"
STATE_FILE = env("STATE_FILE", "state.json")
MAX_CODES = 6

Page = namedtuple("Page", "status url text")

VENUE_RES = [
    re.compile(r'"venueName"\s*:\s*"((?:[^"\\]|\\.){2,150})"'),
    re.compile(r'"VenueName"\s*:\s*"((?:[^"\\]|\\.){2,150})"'),
    re.compile(r'data-venue-name="([^"]{2,150})"'),
]
BOOK_BUTTON_RE = re.compile(r'>\s*Book tickets\s*<|"Book tickets"', re.I)
BLOCK_MARKERS = (
    "just a moment", "cf-chl", "attention required", "access denied",
    "px-captcha", "request unsuccessful", "are you a robot",
)


def log(msg: str) -> None:
    print(f"[{datetime.now(IST):%Y-%m-%d %H:%M:%S} IST] {msg}", flush=True)


def resolve_dates(raw: list[str]) -> list[str]:
    today = datetime.now(IST)
    out = []
    for d in raw:
        if d.lower() == "today":
            out.append(today.strftime("%Y%m%d"))
        elif d.lower() == "tomorrow":
            out.append((today + timedelta(days=1)).strftime("%Y%m%d"))
        else:
            out.append(d)
    return out


def pretty_date(d: str) -> str:
    try:
        return datetime.strptime(d, "%Y%m%d").strftime("%d %b").lstrip("0")
    except ValueError:
        return d


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
class Fetcher:
    def __init__(self) -> None:
        if cffi_requests:
            self.s = cffi_requests.Session(impersonate="chrome")
        else:
            self.s = requests.Session()
            self.s.headers["User-Agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        self.s.headers.update({"Accept-Language": "en-IN,en;q=0.9"})
        self.ok = self.blocked = self.failed = 0
        self._first = True

    def get(self, url: str) -> Page | None:
        if not self._first:
            time.sleep(random.uniform(2, 4))  # be gentle with BookMyShow
        self._first = False
        for attempt in range(2):
            try:
                r = self.s.get(url, timeout=30, allow_redirects=True)
                text, status, final = r.text or "", r.status_code, str(r.url)
                low = text[:30000].lower()
                if status in (403, 429, 503) or (
                    len(text) < 30000 and any(m in low for m in BLOCK_MARKERS)
                ):
                    self.blocked += 1
                    log(f"BLOCKED ({status}) {url}")
                    return None
                if status == 404:
                    self.ok += 1
                    log(f"404 {url}")
                    return Page(404, final, "")
                if status >= 400:
                    self.failed += 1
                    log(f"HTTP {status} {url}")
                    return None
                self.ok += 1
                log(f"{status} {url} -> {final} ({len(text)} bytes)")
                return Page(status, final, text)
            except Exception as e:  # network hiccup: retry once
                log(f"Error fetching {url}: {e}")
                if attempt == 0:
                    time.sleep(3)
        self.failed += 1
        return None


def first_page(f: Fetcher, urls: list[str]) -> Page | None:
    """Try URL formats in order; skip ones that 404."""
    for url in urls:
        page = f.get(url)
        if page is not None and page.status != 404:
            return page
    return None


def movie_urls(code: str) -> list[str]:
    return [f"{BASE}/movies/{CITY}/{SLUG}/{code}", f"{BASE}/{CITY}/movies/{SLUG}/{code}"]


def buyticket_urls(code: str, date: str) -> list[str]:
    return [
        f"{BASE}/movies/{CITY}/{SLUG}/buytickets/{code}/{date}",
        f"{BASE}/buytickets/{SLUG}-{CITY}/movie-{REGION.lower()}-{code}-MT/{date}",
    ]


def codes_near_slug(text: str) -> set[str]:
    return set(re.findall(rf"{re.escape(SLUG)}[^\"'\s<>]{{0,120}}?(ET\d{{8}})", text))


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def extract_venues(text: str) -> set[str]:
    names = set()
    for rx in VENUE_RES:
        for raw in rx.findall(text):
            try:
                name = json.loads(f'"{raw}"')
            except Exception:
                name = raw
            name = html.unescape(name).strip()
            if 2 < len(name) < 150:
                names.add(name)
    return names


def date_in_url(url: str) -> str | None:
    m = re.search(r"/(20\d{6})(?:[/?#]|$)", url)
    return m.group(1) if m else None


def discover_codes(f: Fetcher) -> set[str]:
    codes: set[str] = set()
    for url in (f"{BASE}/explore/upcoming-movies-{CITY}", f"{BASE}/explore/movies-{CITY}"):
        page = f.get(url)
        if page and page.text:
            codes |= codes_near_slug(page.text)
    log(f"Discovered event codes: {sorted(codes) or 'none'}")
    return codes


def check_movie_page(f: Fetcher, code: str) -> tuple[bool, set[str], str | None]:
    """Returns (book button visible?, related event codes, movie page URL)."""
    page = first_page(f, movie_urls(code))
    if not page:
        return False, set(), None
    return bool(BOOK_BUTTON_RE.search(page.text)), codes_near_slug(page.text), page.url


def check_showtimes(f: Fetcher, code: str, dates: list[str]) -> dict[str, set[str]]:
    venues: dict[str, set[str]] = {}
    for date in dates:
        page = first_page(f, buyticket_urls(code, date))
        if not page:
            continue
        if "buytickets" not in page.url.lower():
            log(f"  {code} {date}: redirected away from booking page -> not open")
            continue
        found = extract_venues(page.text)
        actual = date_in_url(page.url) or date
        log(f"  {code} {date}: {len(found)} cinema(s) found (page date {actual})")
        for v in found:
            venues.setdefault(v, set()).add(actual)
    return venues


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #
DRY_RUN = False


def send_whatsapp(text: str) -> bool:
    if DRY_RUN:
        log(f"[dry-run] WhatsApp:\n{text}")
        return True
    phone, key = env("CALLMEBOT_PHONE"), env("CALLMEBOT_APIKEY")
    if not (phone and key):
        log("WhatsApp not configured (CALLMEBOT_PHONE / CALLMEBOT_APIKEY missing)")
        return False
    try:
        r = requests.get(
            "https://api.callmebot.com/whatsapp.php",
            params={"phone": phone, "text": text[:1500], "apikey": key},
            timeout=40,
        )
        ok = r.status_code == 200 and "error" not in r.text.lower()[:300]
        log(f"WhatsApp {'sent' if ok else 'FAILED'}: {r.status_code} {r.text[:200]!r}")
        return ok
    except Exception as e:
        log(f"WhatsApp error: {e}")
        return False


def make_call(spoken: str) -> bool:
    if DRY_RUN:
        log(f"[dry-run] Phone call: {spoken}")
        return True
    sid, token = env("TWILIO_ACCOUNT_SID"), env("TWILIO_AUTH_TOKEN")
    from_, to = env("TWILIO_FROM_NUMBER"), env("CALL_TO_NUMBER")
    if not all((sid, token, from_, to)):
        log("Phone call not configured (Twilio secrets missing)")
        return False
    twiml = (
        '<Response><Pause length="1"/>'
        f'<Say loop="3">{xml_escape(spoken)}</Say></Response>'
    )
    try:
        r = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json",
            auth=(sid, token),
            data={"To": to, "From": from_, "Twiml": twiml},
            timeout=40,
        )
        ok = r.status_code in (200, 201)
        log(f"Call {'placed' if ok else 'FAILED'}: {r.status_code} {r.text[:200]!r}")
        return ok
    except Exception as e:
        log(f"Call error: {e}")
        return False


def tickets_message(venues: dict[str, set[str]], matched: set[str], link: str) -> str:
    lines = [f"🎬 {MOVIE_TITLE}: BOOKINGS OPEN in {CITY.title()}!", ""]
    ordered = sorted(venues, key=lambda v: (v not in matched, v.lower()))
    lines.append(f"Cinemas with shows ({len(venues)}):")
    for v in ordered[:25]:
        star = "⭐ " if VENUE_FILTER and v in matched else ""
        dates = ", ".join(pretty_date(d) for d in sorted(venues[v]))
        lines.append(f"• {star}{v} ({dates})")
    if len(ordered) > 25:
        lines.append(f"…and {len(ordered) - 25} more")
    lines += ["", f"Book now: {link}"]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
DEFAULT_STATE = {
    "notified_venues": [],
    "call_made": False,
    "possible_alert_sent": False,
    "discovered_codes": [],
    "consecutive_errors": 0,
    "error_alert_sent": False,
    "last_heartbeat": "",
}


def load_state() -> dict:
    try:
        with open(STATE_FILE) as fh:
            return {**DEFAULT_STATE, **json.load(fh)}
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(DEFAULT_STATE)


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.write("\n")


def write_summary(lines: list[str]) -> None:
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as fh:
            fh.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    global DRY_RUN
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-notify", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    DRY_RUN = args.dry_run

    if args.test_notify:
        wa = send_whatsapp(f"✅ Test from your {MOVIE_TITLE} ticket watcher. WhatsApp alerts work!")
        call = make_call(f"This is a test call from your {MOVIE_TITLE} ticket watcher. Calls work.")
        log(f"Test results: WhatsApp={'OK' if wa else 'FAILED'}, Call={'OK' if call else 'FAILED'}")
        return 0 if (wa or call) else 1

    now = datetime.now(IST)
    today = now.strftime("%Y%m%d")
    dates = resolve_dates(DATES_RAW)
    if dates and all(d < today for d in dates):
        log("All watched dates are in the past. Nothing to do; you can disable the workflow.")
        return 0

    state = load_state()
    f = Fetcher()

    # Which BookMyShow event codes to watch
    queue = list(dict.fromkeys(EVENT_CODES + state["discovered_codes"]))
    if not queue:
        queue = sorted(discover_codes(f))
    seen: list[str] = []
    all_venues: dict[str, set[str]] = {}
    book_button = False
    link = f"{BASE}/explore/movies-{CITY}"

    while queue and len(seen) < MAX_CODES:
        code = queue.pop(0)
        if code in seen:
            continue
        seen.append(code)
        log(f"Checking event {code}")
        button, related, page_url = check_movie_page(f, code)
        book_button |= button
        if page_url and link.endswith(f"movies-{CITY}"):
            link = page_url
        queue += [c for c in sorted(related) if c not in seen and c not in queue]
        for v, ds in check_showtimes(f, code, dates).items():
            all_venues.setdefault(v, set()).update(ds)

    state["discovered_codes"] = [c for c in seen if c not in EVENT_CODES]
    matched = {v for v in all_venues if not VENUE_FILTER or any(k in v.lower() for k in VENUE_FILTER)}
    new = matched - set(state["notified_venues"])

    log(f"Event codes checked: {seen or 'none'}")
    log(f"'Book tickets' button seen: {book_button}")
    log(f"Cinemas found: {len(all_venues)} (matching filter: {len(matched)}, new: {len(new)})")

    # --- Alerts -------------------------------------------------------------
    if new:
        send_whatsapp(tickets_message(all_venues, matched, link))
        if not state["call_made"]:
            sample = ", ".join(sorted(new)[:3])
            if make_call(
                f"Alert! {MOVIE_TITLE} bookings are open in {CITY}. "
                f"Shows at {len(matched)} cinemas, including {sample}. "
                "Check WhatsApp and book now."
            ):
                state["call_made"] = True
        state["notified_venues"] = sorted(set(state["notified_venues"]) | new)
    elif book_button and not all_venues and not state["possible_alert_sent"]:
        send_whatsapp(
            f"⚠️ {MOVIE_TITLE}: the 'Book tickets' button appeared on BookMyShow "
            f"({CITY.title()}), but I couldn't read the cinema list. Check now:\n{link}"
        )
        if CALL_ON_POSSIBLE and not state["call_made"]:
            if make_call(f"{MOVIE_TITLE} bookings may be open in {CITY}. Check BookMyShow now."):
                state["call_made"] = True
        state["possible_alert_sent"] = True

    # --- Health tracking ----------------------------------------------------
    if f.ok == 0:
        state["consecutive_errors"] += 1
        log(f"No successful requests this run ({state['consecutive_errors']} in a row)")
        if state["consecutive_errors"] >= ERROR_ALERT_AFTER and not state["error_alert_sent"]:
            send_whatsapp(
                f"🚧 Ticket watcher problem: BookMyShow has blocked or failed every check "
                f"for the last {state['consecutive_errors']} runs. Check manually for now: {link}"
            )
            state["error_alert_sent"] = True
    else:
        if state["error_alert_sent"]:
            send_whatsapp("✅ Ticket watcher is working again.")
        state["consecutive_errors"] = 0
        state["error_alert_sent"] = False

    # --- Daily "still alive" message ---------------------------------------
    if (
        HEARTBEAT_HOUR_IST.lower() != "off"
        and not state["notified_venues"]
        and now.hour >= int(HEARTBEAT_HOUR_IST)
        and state["last_heartbeat"] != today
    ):
        health = "checks are running fine" if f.ok else "BookMyShow is blocking checks right now"
        send_whatsapp(f"👀 Daily update: no {MOVIE_TITLE} bookings in {CITY.title()} yet; {health}.")
        state["last_heartbeat"] = today

    write_summary([
        f"### {MOVIE_TITLE} watcher, {now:%d %b %H:%M} IST",
        f"- Event codes: {', '.join(seen) or 'none found yet'}",
        f"- Book button seen: {book_button}",
        f"- Cinemas found: {len(all_venues)}",
        f"- Requests ok/blocked/failed: {f.ok}/{f.blocked}/{f.failed}",
        *[f"  - {v}" for v in sorted(all_venues)[:40]],
    ])

    if not DRY_RUN:
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
