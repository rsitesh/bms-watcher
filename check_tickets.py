#!/usr/bin/env python3
"""
Avengers: Doomsday - BookMyShow Hyderabad ticket watcher.

Checks whether bookings are open and which cinemas have shows, then alerts you
by WhatsApp (CallMeBot, free) and a phone call (Twilio). Prasads (or any
cinemas in PRIORITY_VENUES) gets its own call as soon as it opens, and another
whenever new Infinity Vision 3D/PCX showtimes are added there. Built to run
every 8 minutes in GitHub Actions, so it keeps working while your laptop is off.

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
MAX_CODES = int(env("MAX_CODES", "10"))

# Cinemas that get their own phone call the moment they open (substring match,
# case-insensitive). "prasad" matches "Prasads Multiplex" and "Prasads Large Screen".
PRIORITY_VENUES = [v.lower() for v in env_list("PRIORITY_VENUES", "prasad")]
PRIORITY_LABEL = env("PRIORITY_LABEL", "Prasads")
PRIORITY_CALLS = int(env("PRIORITY_CALLS", "1"))  # how many runs in a row to call
CALL_ONLY_FOR_PRIORITY = env("CALL_ONLY_FOR_PRIORITY", "false").lower() == "true"

# Format watch: alert when NEW showtimes in a premium format are added at the
# priority cinema (Prasads). Comma = any of these; "+" inside an item = all of them
# (e.g. "infinity vision+pcx" only matches shows labelled with both).
FORMAT_KEYWORDS = [
    [k.strip().lower() for k in item.split("+") if k.strip()]
    for item in env_list("WATCH_FORMATS", "infinity vision,pcx")
]
FORMAT_LABEL = env("WATCH_FORMAT_LABEL", "Infinity Vision 3D/PCX")
# Event codes whose every show counts as the watched format (e.g. the code in the
# BookMyShow URL after picking "Infinity Vision 3D" as the format).
FORMAT_EVENT_CODES = [c.upper() for c in env_list("FORMAT_EVENT_CODES")]
FORMAT_CALL = env("FORMAT_CALL", "true").lower() == "true"
DEBUG_DIR = env("DEBUG_DIR", "debug")

# Speed: hard cap on how long one run spends talking to BookMyShow
RUN_BUDGET_SECONDS = int(env("RUN_BUDGET_SECONDS", "120"))
REQUEST_TIMEOUT = int(env("REQUEST_TIMEOUT", "15"))

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
        self.ok = self.blocked = self.failed = self.skipped = 0
        self._first = True
        self.deadline = time.monotonic() + RUN_BUDGET_SECONDS

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def get(self, url: str) -> Page | None:
        if self.remaining() <= 0:
            self.skipped += 1
            log(f"Time budget used up, skipping {url}")
            return None
        if not self._first:
            time.sleep(random.uniform(0.8, 1.8))  # still gentle with BookMyShow
        self._first = False
        for attempt in range(2):
            timeout = max(5, min(REQUEST_TIMEOUT, self.remaining()))
            try:
                r = self.s.get(url, timeout=timeout, allow_redirects=True)
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
            except Exception as e:  # network hiccup: retry once if there's time
                log(f"Error fetching {url}: {e}")
                if attempt == 0 and self.remaining() > 20:
                    time.sleep(2)
                    continue
                break
        self.failed += 1
        return None


# Which URL format worked last time ("movie"/"buy" -> index), saved in state so
# later runs skip formats that 404.
URL_PREF: dict[str, int] = {}


def first_page(f: Fetcher, urls: list[str], kind: str) -> Page | None:
    """Try URL formats (last-working one first); skip ones that 404."""
    pref = URL_PREF.get(kind, 0)
    order = [pref] + [i for i in range(len(urls)) if i != pref] if pref < len(urls) else range(len(urls))
    for i in order:
        page = f.get(urls[i])
        if page is not None and page.status != 404:
            URL_PREF[kind] = i
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
def clean_name(raw: str) -> str:
    try:
        name = json.loads(f'"{raw}"')
    except Exception:
        name = raw
    return html.unescape(name).strip()


def extract_venues(text: str) -> set[str]:
    names = set()
    for rx in VENUE_RES:
        for raw in rx.findall(text):
            name = clean_name(raw)
            if 2 < len(name) < 150:
                names.add(name)
    return names


# ---- Show-level parsing (for the format watch) ---------------------------- #
TIME_RE = re.compile(r"(?<![\d:])(0?[1-9]|1[0-2]):([0-5]\d)\s?([AaPp])\.?[Mm]\b")
ISO_TIME_RE = re.compile(r"(?<=[T ])([01]\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?(?!\d)")
END_KEY_RE = re.compile(r"end|close|cutoff|till|until", re.I)


def venue_sections(text: str) -> dict[str, str]:
    """Split the page at each venue name; each venue gets the text up to the next venue."""
    hits = sorted(
        (m.start(), clean_name(m.group(1))) for rx in VENUE_RES for m in rx.finditer(text)
    )
    out: dict[str, list[str]] = {}
    for i, (pos, name) in enumerate(hits):
        end = hits[i + 1][0] if i + 1 < len(hits) else len(text)
        out.setdefault(name, []).append(text[pos:min(end, pos + 400_000)])
    return {k: "\n".join(v) for k, v in out.items()}


def find_times(text: str) -> list[tuple[int, str]]:
    """(position, normalized '10:30 PM') for show start times, skipping end times."""
    found = []
    for m in TIME_RE.finditer(text):
        if END_KEY_RE.search(text[max(0, m.start() - 25):m.start()]):
            continue
        found.append((m.start(), f"{int(m.group(1))}:{m.group(2)} {m.group(3).upper()}M"))
    if found:
        return found
    for m in ISO_TIME_RE.finditer(text):  # fallback: 24-hour times like 2026-12-17T22:30
        if END_KEY_RE.search(text[max(0, m.start() - 25):m.start()]):
            continue
        h, mi = int(m.group(1)), m.group(2)
        found.append((m.start(), f"{(h % 12) or 12}:{mi} {'PM' if h >= 12 else 'AM'}"))
    return found


def matches_format(snippet: str) -> bool:
    low = snippet.lower()
    return any(all(k in low for k in group) for group in FORMAT_KEYWORDS)


def _parent_open(text: str, start: int, limit: int) -> int | None:
    depth, i, lo = 0, start, max(0, start - limit)
    while i > lo:
        i -= 1
        c = text[i]
        if c == "}":
            depth += 1
        elif c == "{":
            if depth == 0:
                return i
            depth -= 1
    return None


def _close(text: str, start: int, limit: int) -> int | None:
    depth = 0
    for j in range(start, min(len(text), start + limit)):
        c = text[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return j + 1
    return None


def show_context(text: str, pos: int, prev_pos: int | None, next_pos: int | None) -> str:
    """Text describing one show: the largest JSON object around this time that
    contains no other show time; else (HTML) the text halfway to its neighbours."""
    best, start, limit = None, pos, 6000
    for _ in range(6):
        o = _parent_open(text, start, limit)
        if o is None:
            break
        c = _close(text, o, limit * 2)
        if c is None:
            break
        obj = text[o:c]
        if len({t for _, t in find_times(obj)}) > 1:
            break
        best, start = obj, o
    if best:
        return best
    lo = (prev_pos + pos) // 2 if prev_pos is not None else max(0, pos - 400)
    hi = (pos + next_pos) // 2 if next_pos is not None else pos + 400
    return text[lo:hi]


def parse_shows(section: str, force_format: bool = False) -> dict[str, bool]:
    """{'10:30 PM': is_watched_format} for one cinema's part of the page."""
    times = find_times(section)
    shows: dict[str, bool] = {}
    for i, (pos, t) in enumerate(times):
        prev_pos = times[i - 1][0] if i else None
        next_pos = times[i + 1][0] if i + 1 < len(times) else None
        is_fmt = force_format or matches_format(show_context(section, pos, prev_pos, next_pos))
        shows[t] = shows.get(t, False) or is_fmt
    return shows


def time_sort_key(t: str) -> int:
    hm, ap = t.split()
    h, m = map(int, hm.split(":"))
    return (h % 12 + (12 if ap == "PM" else 0)) * 60 + m


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
    page = first_page(f, movie_urls(code), "movie")
    if not page:
        return False, set(), None
    return bool(BOOK_BUTTON_RE.search(page.text)), codes_near_slug(page.text), page.url


ShowPage = namedtuple("ShowPage", "code req_date date text")


def check_showtimes(
    f: Fetcher, code: str, dates: list[str]
) -> tuple[dict[str, set[str]], list[ShowPage]]:
    """Returns ({venue: dates}, pages fetched successfully, for show-level parsing)."""
    venues: dict[str, set[str]] = {}
    pages: list[ShowPage] = []
    for date in dates:
        page = first_page(f, buyticket_urls(code, date), "buy")
        if not page:
            continue
        if "buytickets" not in page.url.lower():
            log(f"  {code} {date}: redirected away from booking page -> not open")
            pages.append(ShowPage(code, date, date, ""))
            continue
        found = extract_venues(page.text)
        actual = date_in_url(page.url) or date
        log(f"  {code} {date}: {len(found)} cinema(s) found (page date {actual})")
        for v in found:
            venues.setdefault(v, set()).add(actual)
        pages.append(ShowPage(code, date, actual, page.text))
    return venues, pages


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


def is_priority(venue: str) -> bool:
    return any(k in venue.lower() for k in PRIORITY_VENUES)


def tickets_message(
    venues: dict[str, set[str]], matched: set[str], link: str, priority_new: bool
) -> str:
    if priority_new:
        head = f"🎯 {PRIORITY_LABEL.upper()} IS OPEN for {MOVIE_TITLE}!"
    else:
        head = f"🎬 {MOVIE_TITLE}: BOOKINGS OPEN in {CITY.title()}!"
    lines = [head, ""]
    ordered = sorted(venues, key=lambda v: (not is_priority(v), v not in matched, v.lower()))
    lines.append(f"Cinemas with shows ({len(venues)}):")
    for v in ordered[:25]:
        if is_priority(v):
            star = "🎯 "
        elif VENUE_FILTER and v in matched:
            star = "⭐ "
        else:
            star = ""
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
    "priority_calls_made": 0,
    "url_pref": {},
    # format watch
    "format_started": False,
    "format_pages": [],      # code|date pages already seen once (their shows are the baseline)
    "format_shows": [],      # date|time|code of every watched-format show seen so far
    "format_unparsed_alert": False,
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


def describe_shows(keys: set[str], spoken: bool = False) -> str:
    """'17 Dec: 10:30 AM, 2:15 PM; 18 Dec: ...' from date|time|code keys."""
    by_date: dict[str, set[str]] = {}
    for k in keys:
        d, t, _ = k.split("|", 2)
        by_date.setdefault(d, set()).add(t)
    parts = []
    for d in sorted(by_date):
        times = sorted(by_date[d], key=time_sort_key)
        if spoken:
            day = datetime.strptime(d, "%Y%m%d").strftime("%d %B").lstrip("0")
            parts.append(f"{day} at {', '.join(times)}")
        else:
            parts.append(f"{pretty_date(d)}: {', '.join(times)}")
    return ("; " if spoken else "\n").join(parts)


def format_watch(state: dict, pages: list[ShowPage], link: str) -> list[str]:
    """Alert when new watched-format shows (e.g. Infinity Vision 3D/PCX) appear at
    the priority cinema. Returns lines for the run summary."""
    known = set(state["format_shows"])
    baselined = set(state["format_pages"])
    first_run = not state["format_started"]
    all_fmt: set[str] = set()
    new_fmt: set[str] = set()
    mentions = 0
    summary = [f"- {PRIORITY_LABEL} shows parsed (★ = {FORMAT_LABEL}):"]

    for pg in pages:
        page_id = f"{pg.code}|{pg.req_date}"
        here: set[str] = set()
        for venue, section in venue_sections(pg.text).items():
            if not is_priority(venue):
                continue
            shows = parse_shows(section, force_format=pg.code in FORMAT_EVENT_CODES)
            if not shows:  # format named here but no showtimes could be read at all
                mentions += sum(section.lower().count(g[0]) for g in FORMAT_KEYWORDS)
            here |= {f"{pg.date}|{t}|{pg.code}" for t, is_fmt in shows.items() if is_fmt}
            listing = ", ".join(
                f"{t}{'★' if shows[t] else ''}" for t in sorted(shows, key=time_sort_key)
            )
            summary.append(f"  - {pg.code} {pretty_date(pg.date)} · {venue}: {listing or 'no times read'}")
            write_debug(pg, venue, section, shows)
        if page_id in baselined:
            new_fmt |= here - known
        baselined.add(page_id)
        known |= here
        all_fmt |= here

    state["format_pages"] = sorted(baselined)
    state["format_shows"] = sorted(known)
    if not pages:
        return summary

    venue_word = PRIORITY_LABEL
    if first_run:
        current = describe_shows(all_fmt) or "none right now"
        send_whatsapp(
            f"👀 Now tracking {FORMAT_LABEL} shows at {venue_word} for {MOVIE_TITLE}.\n\n"
            f"Currently listed:\n{current}\n\nI'll alert you when new ones are added."
        )
        state["format_started"] = True
    elif new_fmt:
        msg = [
            f"🆕 NEW {FORMAT_LABEL} shows at {venue_word}!",
            "",
            describe_shows(new_fmt),
            "",
            f"All {FORMAT_LABEL} shows now:",
            describe_shows(all_fmt),
            "",
            f"Book now: {link}",
        ]
        send_whatsapp("\n".join(msg))
        if FORMAT_CALL:
            make_call(
                f"Alert! New {FORMAT_LABEL.replace('/', ' ')} shows were added at {venue_word} "
                f"for {MOVIE_TITLE}: {describe_shows(new_fmt, spoken=True)}. Book now."
            )

    if not all_fmt and mentions and not state["format_unparsed_alert"]:
        send_whatsapp(
            f"⚠️ {FORMAT_LABEL} is mentioned in the {venue_word} listing, but I couldn't read the "
            f"showtimes. Please check the app, and see the run summary on GitHub."
        )
        state["format_unparsed_alert"] = True

    summary.append(f"- {FORMAT_LABEL} shows known: {len(known)}; new this run: {len(new_fmt)}")
    log(f"{FORMAT_LABEL} at {venue_word}: {len(all_fmt)} now, {len(new_fmt)} new")
    return summary


def write_debug(pg: ShowPage, venue: str, section: str, shows: dict[str, bool]) -> None:
    """Save what was parsed for the priority cinema, for checking against the app."""
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9]+", "_", f"{pg.code}_{pg.req_date}_{venue}")[:120]
        with open(os.path.join(DEBUG_DIR, f"{name}.txt"), "w") as fh:
            fh.write(f"code={pg.code} requested={pg.req_date} page_date={pg.date} venue={venue}\n")
            fh.write(f"parsed={json.dumps(shows)}\n\n")
            fh.write(section[:300_000])
    except OSError as e:
        log(f"Could not write debug file: {e}")


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

    started = time.monotonic()
    state = load_state()
    URL_PREF.update(state.get("url_pref") or {})
    f = Fetcher()

    # Which BookMyShow event codes to watch
    queue = list(dict.fromkeys(EVENT_CODES + state["discovered_codes"]))
    if not queue:
        queue = sorted(discover_codes(f))
    seen: list[str] = []
    all_venues: dict[str, set[str]] = {}
    book_button = False
    link = f"{BASE}/explore/movies-{CITY}"

    pages: list[ShowPage] = []
    bookings_open = bool(state["notified_venues"])
    while queue and len(seen) < MAX_CODES:
        code = queue.pop(0)
        if code in seen:
            continue
        seen.append(code)
        log(f"Checking event {code}")
        # Showtimes first: they're what matter, so they never get cut by the time budget
        venues_here, pages_here = check_showtimes(f, code, dates)
        pages += pages_here
        for v, ds in venues_here.items():
            all_venues.setdefault(v, set()).update(ds)
        # Once bookings are open, one movie page per run is enough to spot new
        # formats/languages (saves time for the showtime pages)
        if bookings_open and len(seen) > 1:
            continue
        button, related, page_url = check_movie_page(f, code)
        book_button |= button
        if page_url and link.endswith(f"movies-{CITY}"):
            link = page_url
        queue += [c for c in sorted(related) if c not in seen and c not in queue]

    state["discovered_codes"] = [c for c in seen if c not in EVENT_CODES]
    state["url_pref"] = dict(URL_PREF)
    priority = {v for v in all_venues if is_priority(v)}
    matched = priority | {
        v for v in all_venues if not VENUE_FILTER or any(k in v.lower() for k in VENUE_FILTER)
    }
    new = matched - set(state["notified_venues"])
    priority_new = bool(priority & new)

    log(f"Event codes checked: {seen or 'none'}")
    log(f"'Book tickets' button seen: {book_button}")
    log(f"Cinemas found: {len(all_venues)} (matching: {len(matched)}, new: {len(new)}, "
        f"{PRIORITY_LABEL}: {sorted(priority) or 'not yet'})")

    # --- Alerts -------------------------------------------------------------
    if new:
        send_whatsapp(tickets_message(all_venues, matched, link, priority_new))
        state["notified_venues"] = sorted(set(state["notified_venues"]) | new)

    # Priority cinema (Prasads): its own call, even if a general call already happened
    if priority and state["priority_calls_made"] < PRIORITY_CALLS:
        names = " and ".join(sorted(priority)[:2])
        if make_call(
            f"Alert! {PRIORITY_LABEL} bookings are open for {MOVIE_TITLE}. "
            f"Shows are listed at {names}. Book on BookMyShow right now."
        ):
            state["priority_calls_made"] += 1
            state["call_made"] = True
    # Any other cinema: one general call, unless you only want calls for the priority cinema
    elif new and not state["call_made"] and not CALL_ONLY_FOR_PRIORITY:
        sample = ", ".join(sorted(new)[:3])
        if make_call(
            f"Alert! {MOVIE_TITLE} bookings are open in {CITY.title()}. "
            f"Shows at {len(matched)} cinemas, including {sample}. "
            f"{PRIORITY_LABEL} is not open yet. Check WhatsApp and book now."
        ):
            state["call_made"] = True

    # Button visible but cinema list unreadable
    if not new and book_button and not all_venues and not state["possible_alert_sent"]:
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

    # --- Format watch (new Infinity Vision 3D/PCX shows at Prasads) ----------
    format_summary = format_watch(state, pages, link) if FORMAT_KEYWORDS or FORMAT_EVENT_CODES else []

    # --- Daily "still alive" message ---------------------------------------
    if (
        HEARTBEAT_HOUR_IST.lower() != "off"
        and now.hour >= int(HEARTBEAT_HOUR_IST)
        and state["last_heartbeat"] != today
    ):
        health = "checks are running fine" if f.ok else "BookMyShow is blocking checks right now"
        if not state["notified_venues"]:
            send_whatsapp(f"👀 Daily update: no {MOVIE_TITLE} bookings in {CITY.title()} yet; {health}.")
        elif state["format_started"]:
            n = len({k.rsplit("|", 1)[0] for k in state["format_shows"]})
            send_whatsapp(
                f"👀 Daily update: {n} {FORMAT_LABEL} show(s) at {PRIORITY_LABEL} so far; "
                f"still watching for new ones. {health.capitalize()}."
            )
        state["last_heartbeat"] = today

    write_summary([
        f"### {MOVIE_TITLE} watcher, {now:%d %b %H:%M} IST",
        f"- Event codes: {', '.join(seen) or 'none found yet'}",
        f"- Book button seen: {book_button}",
        f"- Cinemas found: {len(all_venues)}",
        f"- {PRIORITY_LABEL}: {', '.join(sorted(priority)) or 'not open yet'}",
        f"- Requests ok/blocked/failed/skipped: {f.ok}/{f.blocked}/{f.failed}/{f.skipped}",
        f"- Check took {time.monotonic() - started:.0f}s",
        *format_summary,
        "- All cinemas:",
        *[f"  - {v}" for v in sorted(all_venues)[:40]],
    ])

    log(f"Done in {time.monotonic() - started:.0f}s "
        f"(ok/blocked/failed/skipped: {f.ok}/{f.blocked}/{f.failed}/{f.skipped})")
    if not DRY_RUN:
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
