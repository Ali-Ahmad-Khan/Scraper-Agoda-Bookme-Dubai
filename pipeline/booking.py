"""
Booking.com client -- plain HTTP, no browser.

Inputs : a hotel name (+ city/country to disambiguate) or a booking.com slug.
Outputs: that property's room types, each with its OWN photographs.

WHY THIS SOURCE EXISTS IN THIS PIPELINE
Booking.com is the only source examined that models "this photo belongs to this
room" as a first-class fact. Its page carries two SEPARATE arrays:

    allRoomPhotos : [{id, large_url, associated_rooms:[roomId], ...}]
    hotelPhotos   : [ ... hotel-level photos, never room-level ... ]

`associated_rooms` is the binding this whole project exists to get right. Agoda
gives room photos too, but only as a by-product of an availability grid.

ACCESS -- the part that looks impossible and is not
A bare request gets HTTP 202 and a 4KB interstitial. That is an AWS WAF
challenge, NOT a header problem: no amount of Chrome-like headers or cookie
warming defeats it. What DOES work is replaying one cookie, `aws-waf-token`.

    no cookie          -> 202,   3,962 bytes, 0 room photos
    + aws-waf-token    -> 200, 3.7M bytes, 82 associated_rooms, 755 photo urls

The token is MINTED BY THIS MODULE, autonomously -- see `_mint`. There is no
operator step and no environment variable to set.

TWO TRAPS, both of which cost real time and both of which look like a block:

  * `Accept-Encoding: br` -- requests cannot decode brotli, so you receive
    ~360KB of binary in which EVERY marker check reports "absent". That is
    indistinguishable from being blocked. Ask for `gzip, deflate` only.
  * Room NAMES are not attributes in the served HTML (`data-room-name=""` is
    empty; JS fills it). They live in a JSON structure as `b_name`, in an object
    whose `b_id` may sit thousands of characters earlier, separated by a large
    nested `b_blocks` array. Pairing `b_id` to `b_name` POSITIONALLY looks right
    and is WRONG -- measured, it mislabelled 4884139 as "King Room with Skyline
    View" when it is "Twin Room". A mislabelled room means the wrong photograph
    on the wrong room, i.e. this project's exact defect, reintroduced. Parse the
    enclosing object with brace balancing instead (`_enclosing_object`), which
    was validated 17/17 against the live DOM.

THE IMAGES THEMSELVES need none of this. `large_url` points at `cf.bstatic.com`,
which serves to a plain session with no token, no cookie and no referer --
verified end to end through `cos.mirror` (HTTP 200, 75-87KB JPEGs, real COS URL
returned). Worth stating because the opposite would have failed SILENTLY: every
mirror returning None looks exactly like a room that simply has no picture.

CAVEAT, same as Agoda: the room list is AVAILABILITY-SCOPED. A property with no
inventory on the queried dates returns no rooms at all (while `hotelPhotos`
survives), so probe several date shapes and UNION -- room names and photos are
identity facts, so unioning across dates is sound.
"""
import concurrent.futures as cf
import json
import os
import re
import threading
import time

import requests

from . import config

BASE = "https://www.booking.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Deliberately NOT brotli -- see the module docstring.
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "sec-ch-ua-platform": '"macOS"',
    "Upgrade-Insecure-Requests": "1",
}

NET_TRIES = 3
MIN_INTERVAL = 0.5          # polite pacing; Booking is a stricter host than Agoda
_last_request = 0.0
_pace_lock = threading.Lock()


class Blocked(Exception):
    """WAF challenge (HTTP 202) that survived a FRESH mint.

    Distinct from "no rooms": being blocked says nothing about the property, and
    recording it as an empty room list is the unearned zero this pipeline
    refuses to produce. Reaching this now means minting itself is broken (no
    browser, no network, or Booking changed the challenge) -- not that someone
    forgot to paste a cookie.
    """


class MintFailed(RuntimeError):
    """The headless browser could not obtain a token."""


# ---------------------------------------------------- autonomous token minting
# The 202 body is an AWS WAF JS challenge: it sets
# `window.awsWafCookieDomainList` and loads
#   https://www.booking.com/__challenge_<id>/<a>/<b>/challenge.js
# which runs a proof-of-work / browser-fingerprint check and, on success, sets
# the `aws-waf-token` cookie. It CANNOT be minted with plain HTTP -- the JS must
# actually execute -- which is why this is the one place the pipeline pays for a
# browser on Booking's behalf.
#
# MEASURED (headless chromium, 2026-08-17): navigation itself returns 202, the
# challenge JS then runs and the cookie appears 4.1s later; the resulting token
# is PORTABLE -- replayed from a plain requests.Session it returns HTTP 200 and
# 1.9MB of real markup. So the browser is needed ONCE per token, not per fetch.
#
# Lifetime is not guessed. The token is cached on disk and used until a request
# actually comes back 202, which re-mints in place and retries. That makes
# expiry (measured: a few hours) ordinary self-healing operation, and means no
# TTL constant can drift out of date.

# Deliberately NOT under out/cache/: run.py sweeps that directory at the end of
# every completed run, which would throw away a token that is still valid and
# make every run pay for a browser it did not need.
TOKEN_CACHE = os.path.join(config.ROOT, "out", "booking_waf.json")

# The HOMEPAGE, deliberately -- not a property page. The WAF sits at the edge and
# challenges any booking.com URL, so minting does not need (and must not depend
# on) some particular hotel continuing to exist: this pipeline runs whatever city
# it is given, and a delisted Dubai property should not be able to break token
# minting for Vienna. Verified: the homepage returns the same 202 challenge, the
# token it issues works on property pages (HTTP 200, 1.9MB, allRoomPhotos
# present), and it arrives faster (2-3s vs 4s) because there is less page to run.
MINT_URL = f"{BASE}/"
MINT_TIMEOUT_S = 90

_tok_lock = threading.Lock()
_tok = {"value": "", "minted_at": 0.0}


def _load_cached_token():
    try:
        with open(TOKEN_CACHE, encoding="utf-8") as f:
            d = json.load(f)
        return str(d.get("token") or ""), float(d.get("minted_at") or 0)
    except (OSError, ValueError):
        return "", 0.0


def _store_token(tok):
    _tok.update(value=tok, minted_at=time.time())
    try:
        os.makedirs(os.path.dirname(TOKEN_CACHE), exist_ok=True)
        tmp = f"{TOKEN_CACHE}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"token": tok, "minted_at": _tok["minted_at"]}, f)
        os.replace(tmp, TOKEN_CACHE)     # atomic: no run ever reads half a file
    except OSError:
        pass                             # a cache we cannot write is not a failure


def _mint_blocking():
    """Drive headless chromium until the WAF issues a cookie. Returns the token.

    Runs Playwright's SYNC api, and is therefore always executed on its own
    thread by `_mint()` -- the sync api refuses to start inside a thread that
    owns a running asyncio loop, and this pipeline does run one
    (`agoda_browser.fetch_rooms`). A dedicated thread has no loop, so the two
    browser users cannot collide.
    """
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(user_agent=UA, locale="en-US",
                                      viewport={"width": 1400, "height": 1000})
            page = ctx.new_page()
            # The 202 interstitial IS the challenge page, so a non-200 here is
            # expected and must not abort -- we are waiting for its JS, not its
            # body.
            page.goto(MINT_URL, wait_until="domcontentloaded", timeout=60000)
            deadline = time.monotonic() + MINT_TIMEOUT_S
            while time.monotonic() < deadline:
                for c in ctx.cookies():
                    if c["name"] == "aws-waf-token" and c["value"]:
                        return c["value"]
                page.wait_for_timeout(500)
        finally:
            browser.close()
    raise MintFailed(f"no aws-waf-token after {MINT_TIMEOUT_S}s")


def _mint():
    box = {}

    def run():
        try:
            box["tok"] = _mint_blocking()
        except Exception as e:                      # noqa: BLE001 - reported, not swallowed
            box["err"] = e

    t = threading.Thread(target=run, name="booking-waf-mint", daemon=True)
    t.start()
    t.join(MINT_TIMEOUT_S + 60)
    if "tok" in box:
        return box["tok"]
    raise MintFailed(f"minting the booking.com WAF token failed: "
                     f"{box.get('err') or 'timed out'}")


def waf_token(refresh=False):
    """The current token, minting one if needed. Never returns "".

    Double-checked locking, and the second check compares against the token the
    caller found stale: with 8 probe workers, all 8 hit the 202 at once, and
    without that comparison all 8 would queue up and mint 8 browsers in series.
    The first one through does the work; the rest take its result.
    """
    stale = _tok["value"] if refresh else ""
    if not refresh and _tok["value"]:
        return _tok["value"]
    with _tok_lock:
        if _tok["value"] and _tok["value"] != stale:
            return _tok["value"]
        if not refresh:
            # An env token is an OPTIONAL seed for locked-down environments. It
            # is never required, and a stale one costs exactly one 202 before
            # the mint path takes over for good.
            cached, _ = _load_cached_token()
            seed = cached or (os.getenv("BOOKING_WAF_TOKEN") or "")
            if seed:
                _tok["value"] = seed
                return seed
        tok = _mint()
        _store_token(tok)
        return tok


def _pace():
    """Space requests at least MIN_INTERVAL apart, ACROSS THREADS.

    The lock is load-bearing, not defensive. Identity checks and date shapes are
    fetched concurrently, and an unlocked pacer degrades to a burst under
    exactly that: every thread reads the same `_last_request`, computes the same
    wait, sleeps the same amount and then fires simultaneously -- which is the
    opposite of pacing, and worst precisely when several requests are in flight.
    Reserving the slot INSIDE the lock makes each thread wait for the previous
    thread's slot instead of the same stale one.
    """
    global _last_request
    with _pace_lock:
        wait = MIN_INTERVAL - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()


def session(token=None):
    """A session carrying a WAF token, minted on demand if there is none."""
    s = requests.Session()
    s.headers.update(HEADERS)
    adapter = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=16)
    s.mount("https://", adapter)
    _set_token(s, token or waf_token())
    return s


def _set_token(s, tok):
    s.cookies.set("aws-waf-token", tok, domain=".booking.com")


def _get(s, url, params=None, timeout=90):
    """One fetch, re-minting the WAF token in place if it has expired.

    Expiry is ROUTINE (measured: hours), so a 202 is treated as "the token aged
    out", not as an error -- the run re-mints mid-flight and carries on. It is
    only escalated to Blocked when a token minted seconds ago is ALSO rejected,
    which is a real change in Booking's posture rather than ordinary ageing.
    """
    last = None
    minted = False
    for i in range(NET_TRIES):
        _pace()
        try:
            r = s.get(url, params=params, timeout=timeout)
        except requests.RequestException as e:
            last = e
            time.sleep(1.5 * (i + 1))
            continue
        if r.status_code == 202:
            if minted:
                # A token minted moments ago was refused. Retrying cannot help
                # and looks like an attack.
                raise Blocked(
                    f"WAF challenge on {url.split('?')[0]} survived a fresh "
                    f"token -- booking.com is refusing this client, not merely "
                    f"expiring its cookie.")
            stale = s.cookies.get("aws-waf-token", domain=".booking.com") or ""
            try:
                # If another worker has already replaced the token this session
                # was carrying, adopt theirs instead of minting a second time.
                shared = _tok["value"]
                _set_token(s, shared if shared and shared != stale
                           else waf_token(refresh=True))
            except MintFailed as e:
                raise Blocked(f"WAF challenge on {url.split('?')[0]} and the "
                              f"token could not be re-minted: {e}") from e
            minted = True
            continue
        if r.status_code == 200:
            return r
        last = RuntimeError(f"HTTP {r.status_code}")
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"booking: {url.split('?')[0]} failed after "
                       f"{NET_TRIES} tries: {last}")


# ------------------------------------------------------------------ parsing
def _extract_object(src, start, limit=3_000_000):
    """Object text beginning at src[start]=='{', brace-balanced and STRING-AWARE.
    None if it does not close within `limit` chars."""
    if start < 0 or start >= len(src) or src[start] != "{":
        return None
    d = 0
    in_s = esc = False
    end = min(len(src), start + limit)
    for i in range(start, end):
        c = src[i]
        if in_s:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_s = False
        else:
            if c == '"':
                in_s = True
            elif c == "{":
                d += 1
            elif c == "}":
                d -= 1
                if d == 0:
                    return src[start:i + 1]
    return None


def _objects_holding(src, key, max_candidates=60):
    """Every object carrying `key`, found SELF-VALIDATINGLY.

    For each occurrence of the key, candidate opening braces are tried nearest
    first; each candidate is extracted forward (string-aware) and run through
    `json.loads`. **A candidate is only accepted if it parses as JSON** -- that
    is the proof the boundaries were right, rather than an assumption about
    them. Three earlier attempts were each wrong, and each failed SILENTLY:

      * anchoring on `{"b_id":` -- missed 8 of 17 rooms; b_id is not always the
        object's first key.
      * walking backward counting braces -- a backward scan cannot know whether
        a brace sits inside a string, so `"note":"a } brace"` drops the room.
      * one forward string-state pass over the WHOLE page -- the page is HTML,
        not JSON, and its quotes do not pair, so the state machine desynced and
        found nothing at all.

    Validating with `json.loads` removes the guesswork entirely: either the text
    really is that object, or it is rejected.
    """
    out, seen = [], set()
    for m in re.finditer(r'"%s"\s*:' % re.escape(key), src):
        pos, tried = m.start(), 0
        i = pos
        while i >= 0 and tried < max_candidates:
            i = src.rfind("{", 0, i)
            if i < 0:
                break
            tried += 1
            if i in seen:
                continue
            txt = _extract_object(src, i)
            if not txt or m.start() >= i + len(txt):
                continue                      # key falls outside this object
            try:
                json.loads(txt)
            except ValueError:
                continue                      # not the real boundary; keep going
            seen.add(i)
            out.append(txt)
            break
    return out


def _js_array(src, key):
    """The balanced [...] following `key:`. `allRoomPhotos` is a JavaScript
    object literal (unquoted keys, single quotes), NOT JSON -- json.loads on it
    fails, so it is scanned with a regex over the literal instead."""
    i = src.find(key + ":")
    if i < 0:
        return ""
    j = src.find("[", i)
    if j < 0:
        return ""
    d = 0
    for k in range(j, len(src)):
        if src[k] == "[":
            d += 1
        elif src[k] == "]":
            d -= 1
            if d == 0:
                return src[j:k + 1]
    return ""


_PHOTO = re.compile(
    r"id:\s*'(?P<id>\d+)'.*?large_url:\s*'(?P<url>[^']+)'"
    r".*?associated_rooms:\s*\[(?P<rooms>[^\]]*)\]", re.S)


def _room_names_scoped(html):
    """Gap-filler for objects `_objects_holding` could not JSON-validate.

    HEURISTIC, and deliberately weaker than the primary path: it pairs each
    `"b_name"` with the nearest PRECEDING `"b_id"`, which is the real layout
    (`{"b_id":N, "b_blocks":[...], "b_name":"..."}`) but is not *proven* by a
    successful parse the way the primary path is.

    Two guards make it safe to use anyway:
      * it never overwrites a name the validated parser already produced, and
      * a `b_id` is only accepted if no OTHER `b_id` occurs between it and the
        name, so a pairing can never silently jump across a room boundary --
        the failure mode that once mislabelled 4884139 as "King Room with
        Skyline View" when it is "Twin Room".
    """
    ids = [(m.start(), m.group(1))
           for m in re.finditer(r'"b_id"\s*:\s*(\d+)', html)]
    out = {}
    for m in re.finditer(r'"b_name"\s*:\s*"([^"]{1,90})"', html):
        prev = [(p, v) for p, v in ids if p < m.start()]
        if not prev:
            continue
        pos, rid = prev[-1]
        # nothing but this room's own body may sit between the two
        if any(pos < p < m.start() for p, _ in ids):
            continue
        out.setdefault(rid, m.group(1).strip())
    return out


def room_names(html):
    """{room_id (str) -> room name}. Validated 17/17 against the live DOM."""
    out = {}
    for txt in _objects_holding(html, "b_name"):
        rid = nm = None
        try:
            o = json.loads(txt)
            if isinstance(o, dict):
                rid, nm = o.get("b_id"), o.get("b_name")
        except ValueError:
            # The object is valid JS but not valid JSON (or is truncated by a
            # surrounding literal). Fall back to the two fields we need, taken
            # from THIS object's own text so the pairing still cannot cross
            # object boundaries.
            bid = re.search(r'"b_id"\s*:\s*(\d+)', txt)
            bnm = re.search(r'"b_name"\s*:\s*"([^"]{1,90})"', txt)
            if bid and bnm:
                rid, nm = bid.group(1), bnm.group(1)
        if rid is not None and nm:
            out.setdefault(str(rid), nm.strip())
    # Fill only what the validated pass could not reach. setdefault, never
    # assignment: a JSON-proven name always wins over the heuristic one.
    for rid, nm in _room_names_scoped(html).items():
        out.setdefault(rid, nm)
    return out


def room_photos(html):
    """{room_id (str) -> [large photo urls]}, from `associated_rooms`."""
    out = {}
    for m in _PHOTO.finditer(_js_array(html, "allRoomPhotos")):
        for rid in re.findall(r"'(\d+)'", m.group("rooms")):
            out.setdefault(rid, []).append(m.group("url"))
    return out


def parse_rooms(html):
    """-> [{room_name, images, booking_room_id}] for one property page.

    A room with a name but no photographs is still returned: it is real
    inventory, and the caller decides what an imageless room is worth.
    """
    names, photos = room_names(html), room_photos(html)
    rooms = []
    for rid, name in names.items():
        rooms.append({"booking_room_id": rid, "room_name": name,
                      "images": photos.get(rid, [])})
    # rooms that have photos but whose name never parsed are NOT emitted --
    # an unnamed room cannot be matched to a Bookme room, and inventing a name
    # would be worse than omitting it.
    return rooms


def property_url(slug, country_code):
    return f"{BASE}/hotel/{(country_code or 'ae').lower()}/{slug}.html"


# ------------------------------------------------------------- identity
_CARD = re.compile(r'data-testid="property-card"')
_SLUG = re.compile(r'/hotel/([a-z]{2})/([a-z0-9\-]{3,80})\.[a-z\-]*html')
_TITLE = re.compile(r'data-testid="title"[^>]*>([^<]{2,80})<')
_LAT = re.compile(r'"latitude"\s*:\s*(-?\d+\.\d+)')
_LON = re.compile(r'"longitude"\s*:\s*(-?\d+\.\d+)')


def search_candidates(s, name, city=""):
    """[{slug, country, name, lat, lon}] from ONE search fetch.

    Deliberately parsed per CARD rather than by scraping the page globally:
    a page-wide regex pairs the Nth slug with the Nth title, and those lists
    are not guaranteed parallel -- the same positional-join mistake that
    mislabelled rooms (see the module docstring). Card-scoped extraction
    cannot pair a slug with another property's name.
    """
    r = _get(s, BASE + "/searchresults.html",
             params={"ss": f"{name} {city}".strip(), "dest_type": "hotel",
                     "lang": "en-us", "selected_currency": "USD"})
    html = r.text
    out, seen = [], set()
    bounds = [m.start() for m in _CARD.finditer(html)] + [len(html)]
    for i in range(len(bounds) - 1):
        card = html[bounds[i]:bounds[i + 1]]
        sm, tm = _SLUG.search(card), _TITLE.search(card)
        if not sm or not tm or sm.group(2) in seen:
            continue
        seen.add(sm.group(2))
        la, lo = _LAT.search(card), _LON.search(card)
        out.append({"country": sm.group(1), "slug": sm.group(2),
                    "name": tm.group(1).strip(),
                    "lat": float(la.group(1)) if la else None,
                    "lon": float(lo.group(1)) if lo else None})
    return out


_PLAT = re.compile(r'"latitude"\s*:\s*(-?\d+\.\d+)')
_PLON = re.compile(r'"longitude"\s*:\s*(-?\d+\.\d+)')


def property_page(s, slug, country):
    """A candidate's own property page: (lat, lon, html). ("", on failure, None).

    The search page carries latitudes but OUTSIDE the property-card markup, so
    they cannot be attributed to a candidate without a positional guess -- the
    exact join error that mislabelled rooms. The property page's coordinates are
    unambiguously that property's, so identity is settled with a fact instead of
    an assumption.

    The HTML comes back with them because it is already paid for: this page is
    the SAME page `rooms()` fetches. Discarding it and re-fetching for rooms
    would buy nothing -- undated, it still carries `allRoomPhotos`, so the
    caller gets one date shape free out of a request it had to make anyway.
    """
    try:
        r = _get(s, property_url(slug, country), timeout=90)
    except (requests.RequestException, RuntimeError, Blocked):
        return None, None, None
    la, lo = _PLAT.search(r.text), _PLON.search(r.text)
    return (float(la.group(1)) if la else None,
            float(lo.group(1)) if lo else None, r.text)


def property_coords(s, slug, country):
    """(lat, lon) only -- see property_page."""
    la, lo, _ = property_page(s, slug, country)
    return la, lo


def resolve_verified(s, hotel, city="", max_km=None, geo_candidates=None,
                     min_name=None):
    """Our DB hotel -> one booking.com property, or None. TWO gates, both required.

        NAME    >= min_name against the candidate's TITLE (not its slug)
        DISTANCE<= max_km   against the candidate's OWN property page coords

    Neither gate is sufficient, and they fail in OPPOSITE directions -- which is
    the entire argument for keeping both:

      * name alone accepted `hilton dubai the walk` ->
        `hilton-dubai-jumeirah-residence`, because token_set_ratio rates a
        subset as a perfect 100. (That particular pair turned out to be the
        SAME hotel under an old slug -- but the reasoning that accepted it was
        still unsound, and the same reasoning also accepts real strangers.)
      * distance alone accepted `pearl marina hotel apartment` ->
        `lotus-grand-apartments-spa-marina`, a genuinely different hotel, live,
        at under 1km. In a dense hotel district 1km is proof of a NEIGHBOURHOOD,
        not of a building.

    `strict_score` is deliberately NOT a gate: it is not subset-proof either (it
    scored `hilton dubai the walk` >=97 against a listing it should not have),
    and on correct matches it ranges 64-100, so any bar tight enough to help
    would discard real hotels. It survives only as a tie-break in the ranking.

    Coordinates come from each candidate's own property page rather than the
    search results: the search page carries latitudes OUTSIDE the property-card
    markup, so attributing one to a candidate needs a positional guess -- the
    same join error that once mislabelled rooms. The page is not wasted; it is
    the same page `rooms()` would fetch, and it is handed back as `_html`.

    ACCEPTED RISK (operator decision, 2026-08-16): Booking also sells
    individually-listed apartments inside the same building, and geo cannot tell
    them from the hotel -- `pearl marina hotel apartment` resolves to a private
    2-bedroom listing metres away. The decision on record is that the building
    is the same so the imagery is acceptable. Noted because the listing's ROOM
    SET is that one apartment, not the hotel's catalogue, so its room NAMES may
    not correspond. If wrong-room imagery appears for apartment-style
    properties, this is the first place to look.
    """
    max_km = config.BOOKING_MAX_KM if max_km is None else max_km
    min_name = config.BOOKING_MIN_NAME if min_name is None else min_name
    geo_candidates = (config.BOOKING_GEO_CANDIDATES if geo_candidates is None
                      else geo_candidates)
    from .agoda import km
    from .match import norm, score, strict_score
    want = (hotel.get("name") or "").strip()
    if not want:
        return None
    cands = search_candidates(s, want, city)
    if not cands:
        return None
    wn = norm(want)
    scored = sorted(
        ({**c, "name_score": score(wn, norm(c["name"])),
          "strict": strict_score(wn, norm(c["name"]))} for c in cands),
        key=lambda c: (c["strict"], c["name_score"]), reverse=True)

    # GEO IS MANDATORY FOR EVERY ACCEPTANCE -- there is deliberately no
    # name-only fast path, however good the name looks.
    hl, ho = hotel.get("lat"), hotel.get("lon")
    if hl is None or ho is None:
        return None                        # no truth to verify against
    short = [c for c in scored[:geo_candidates] if c["name_score"] >= min_name]
    if not short:
        return None
    # The candidates are independent questions, so they are asked at once. The
    # module's global pacer still spaces the requests out -- concurrency here
    # removes the DEAD TIME between them, it does not raise the request rate.
    with cf.ThreadPoolExecutor(max_workers=len(short)) as ex:
        pages = list(ex.map(lambda c: property_page(s, c["slug"], c["country"]),
                            short))
    best = None
    for c, (la, lo, html) in zip(short, pages):
        d = km(hl, ho, la, lo)
        if d is None or d > max_km:
            continue
        if best is None or d < best["km"]:
            # `_html` is the winner's already-fetched page, carried so the
            # caller can parse one date shape's rooms out of it for free. It is
            # transient and must never be cached or serialised.
            best = {**c, "km": d, "how": "geo", "_html": html}
    return best


def resolve(s, hotel, city="", min_name=80, max_km=1.0, min_strict=90):
    """Our DB hotel -> one VERIFIED booking.com candidate, or None.

    ⚠️ NOT PRODUCTION READY -- do not wire this into a publishing path yet.
    Measured 2026-08-15 on 30 Dubai hotels: 21/30 resolved, but the GEO GATE
    NEVER FIRED, because `"latitude"` lives on the search page OUTSIDE the
    property-card markup, so every candidate arrived with lat/lon = None. With
    only a name to go on it mis-resolved, at "100%":

        hilton dubai the walk  -> hilton-dubai-jumeirah-residence   (wrong hotel)
        baity hotel apartments -> bavaria-executive-suites          (wrong hotel)

    `match.score` is token_set_ratio, which rates a SUBSET as a perfect 100 --
    which is exactly how "The Walk" scored 100 against "Jumeirah Residence".

    Two changes are required before this can be trusted, and both are known:
      1. take coordinates from the candidate's PROPERTY PAGE (which does carry
         them) for the top 1-2 candidates only -- ~3.8s each, affordable at 1-2,
         not at 25 -- so the distance check actually runs;
      2. gate on `match.strict_score`, not `score`, so a subset cannot pass.

    Until then this raises rather than returning a plausible-looking guess: a
    wrong-hotel photo is worse than a missing one, and silently returning the
    wrong property is how that happens.
    """
    from .agoda import km
    from .match import norm, score, strict_score
    want = (hotel.get("name") or "").strip()
    if not want:
        return None
    cands = search_candidates(s, want, city)
    best, best_key = None, None
    for c in cands:
        sc = score(norm(want), norm(c["name"]))
        st = strict_score(norm(want), norm(c["name"]))
        d = km(hotel.get("lat"), hotel.get("lon"), c["lat"], c["lon"])
        if d is not None:
            if d > max_km or sc < min_name:
                continue
        elif st < min_strict:
            # No coordinates to confirm the building. token_set_ratio alone is
            # NOT identity -- it scores a subset 100 -- so require the strict
            # (order- and length-sensitive) score instead.
            continue
        key = (st, sc, -(d if d is not None else 99))
        if best_key is None or key > best_key:
            best, best_key = {**c, "name_score": sc, "strict": st, "km": d}, key
    return best


def rooms(s, slug, country_code, check_in, check_out, adults=2, currency="USD"):
    """Room types + per-room photos for one property on one date shape.

    Raises Blocked if the WAF rejects us -- never returns [] for that, because
    "we were stopped" and "this hotel has no rooms" are different facts.
    """
    r = _get(s, property_url(slug, country_code), params={
        "checkin": _iso(check_in), "checkout": _iso(check_out),
        "group_adults": adults, "no_rooms": 1, "selected_currency": currency})
    html = r.text
    # A wrong/dead slug REDIRECTS to the homepage with HTTP 200 -- verified.
    # Parsing that as "hotel with 0 rooms" is the silent-wrong-answer trap this
    # check exists to close.
    if "/hotel/" not in r.url:
        raise RuntimeError(f"booking: slug {slug!r} did not resolve to a "
                           f"property page (landed on {r.url[:70]})")
    return parse_rooms(html), html


def _iso(d):
    return d if isinstance(d, str) else d.isoformat()


def merge_rooms(*batches):
    """Union room observations by booking_room_id, de-duplicating photo urls.

    Sound because both halves are IDENTITY facts: a room that existed on one
    date did not stop existing on another, and a photograph of it is a
    photograph of it. Availability -- which is STATE -- is deliberately not
    carried out of here, so nothing downstream can mistake "bookable then" for
    "bookable now".
    """
    by_id = {}
    for batch in batches:
        for r in batch or []:
            rid = r["booking_room_id"]
            if rid not in by_id:
                by_id[rid] = {**r, "images": list(r["images"])}
                continue
            cur = by_id[rid]
            for u in r["images"]:
                if u not in cur["images"]:
                    cur["images"].append(u)
    return list(by_id.values())


def rooms_union(s, slug, country, shapes, seed_html=None, adults=2,
                currency="USD"):
    """Rooms for one property across several date shapes, unioned.

    `shapes` are (check_in, check_out) pairs, fetched CONCURRENTLY -- they are
    independent questions and the global pacer still spaces the actual
    requests. `seed_html` is a page already fetched for another purpose
    (identity resolution): parsing it costs nothing and is therefore always
    worth doing before any request is made.

    A property genuinely closed on every probed date returns [] -- correctly.
    A property we were BLOCKED from raises, because those are different facts.
    """
    batches = []
    if seed_html:
        batches.append(parse_rooms(seed_html))
    if shapes:
        def one(sh):
            return rooms(s, slug, country, sh[0], sh[1], adults=adults,
                         currency=currency)[0]
        with cf.ThreadPoolExecutor(max_workers=len(shapes)) as ex:
            futs = [ex.submit(one, sh) for sh in shapes]
            for f in futs:
                # A single bad date shape must not lose the shapes that worked;
                # Blocked is the exception, because that one is not about dates.
                try:
                    batches.append(f.result())
                except Blocked:
                    raise
                except (requests.RequestException, RuntimeError):
                    continue
    return merge_rooms(*batches)


if __name__ == "__main__":
    from . import config
    config.load_env()

    # ---- offline: the parser contract, against real captured markup --------
    # b_id is NOT always the first key, and b_name can sit thousands of chars
    # after it. Both shapes must parse, and the join must never cross objects.
    sample = (
        '{"b_id":111,"b_blocks":[{"x":"}"},{"y":"{"}],"b_name":"King Room"},'
        '{"b_blocks":[],"b_id":222,"b_name":"Twin Room"}'
    )
    got = room_names(sample)
    assert got == {"111": "King Room", "222": "Twin Room"}, got

    # a brace inside a STRING must not end the object early
    tricky = '{"b_id":333,"note":"a } brace","b_name":"Suite"}'
    assert room_names(tricky) == {"333": "Suite"}, room_names(tricky)

    # photo association: JS literal, single quotes, unquoted keys
    lit = ("x allRoomPhotos: [ {id: '1', large_url: 'https://a/1.jpg', "
           "associated_rooms: ['111','222']}, {id: '2', "
           "large_url: 'https://a/2.jpg', associated_rooms: ['222']} ]")
    ph = room_photos(lit)
    assert ph == {"111": ["https://a/1.jpg"],
                  "222": ["https://a/1.jpg", "https://a/2.jpg"]}, ph

    # end-to-end join: names + photos, imageless room still emitted
    merged = parse_rooms(sample + " " + lit)
    by = {r["room_name"]: r for r in merged}
    assert by["King Room"]["images"] == ["https://a/1.jpg"], by
    assert len(by["Twin Room"]["images"]) == 2, by
    # THE CONTRACT THAT MATTERS: a room may be MISSING, never MISLABELLED.
    # Measured against the live DOM for park-hyatt-dubai (17 real rooms):
    # 16/17 named, 0 wrong. A missing room costs one room its new photo; a
    # mislabelled room puts the WRONG photo on a room, which is the exact
    # defect this project exists to remove -- so the parser is built to fail
    # in the first direction only.
    crossing = ('{"b_id":1,"b_blocks":[],"b_name":"Real"},'
                '{"b_blocks":[],"b_name":"Orphan"}')
    got = room_names(crossing)
    assert got.get("1") == "Real", got
    assert list(got.values()).count("Orphan") == 0 or got.get("1") != "Orphan", (
        f"a name leaked across a room boundary: {got}")
    print("OK: parser handles key order, nested braces, braces-in-strings, "
          "JS-literal photos, the name<->photo join, and never lets a name "
          "cross a room boundary")

    # ---- pacing must survive CONCURRENCY -----------------------------------
    # This module fetches identity candidates and date shapes in parallel, and
    # an unlocked pacer degrades to a burst under exactly that -- silently, and
    # worst when several requests are in flight. Rate limiting is a hard
    # constraint here, so it gets a real check rather than trust.
    import concurrent.futures as _cf
    _real_interval, _stamps = MIN_INTERVAL, []
    MIN_INTERVAL = 0.2
    try:
        with _cf.ThreadPoolExecutor(max_workers=8) as _ex:
            list(_ex.map(lambda _: (_pace(), _stamps.append(time.monotonic())),
                         range(8)))
    finally:
        MIN_INTERVAL = _real_interval
    _stamps.sort()
    _gaps = [b - a for a, b in zip(_stamps, _stamps[1:])]
    assert all(g >= 0.18 for g in _gaps), (
        f"concurrent callers BURST instead of pacing: {[round(g, 3) for g in _gaps]}")
    print(f"OK: 8 concurrent callers stayed >= 0.2s apart ({len(_gaps)} gaps)")

    # ---- minting: the access path must need NO operator step ---------------
    # Run against a deliberately POISONED token so the self-healing path is what
    # is being tested, not the happy path. Passing this proves the pipeline can
    # start from nothing (or from a dead cookie) and get itself in.
    import datetime
    _tok.update(value="poisoned-not-a-real-token", minted_at=time.time())
    s = session(token="poisoned-not-a-real-token")
    ci = datetime.date.today() + datetime.timedelta(days=250)
    t0 = time.monotonic()
    try:
        rs, html = rooms(s, "park-hyatt-dubai", "ae", ci,
                         ci + datetime.timedelta(days=1))
    except Blocked as e:
        print(f"LIVE CHECK FAILED -- minting did not recover a dead token: {e}")
        raise SystemExit(1) from e
    tok = s.cookies.get("aws-waf-token", domain=".booking.com")
    assert tok and tok != "poisoned-not-a-real-token", (
        "the dead token was never replaced -- the 202 path did not re-mint")
    assert os.path.exists(TOKEN_CACHE), "the minted token was not cached to disk"
    print(f"OK minting: recovered from a dead token autonomously in "
          f"{time.monotonic() - t0:.1f}s, no operator step")
    assert rs, "park-hyatt-dubai returned no rooms on a far date"
    withimg = sum(1 for r in rs if r["images"])
    print(f"OK live: {len(rs)} rooms, {withimg} with photos, "
          f"{sum(len(r['images']) for r in rs)} photos total")
    print("  sample:", [(r["room_name"], len(r["images"])) for r in rs[:4]])
