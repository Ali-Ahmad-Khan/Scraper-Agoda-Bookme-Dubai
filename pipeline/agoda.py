"""
Agoda client -- plain HTTP, no browser.

Inputs : a hotel name (to resolve) or an Agoda property id + dates.
Outputs: candidate property matches, and the full room grid with per-room images.

Why no Playwright: Agoda's property page renders the room grid in React with
obfuscated class names, but the page itself is fed by a public JSON endpoint --
  /api/cronos/property/BelowFoldParams/GetSecondaryData?hotel_id=..&checkIn=..
which needs no session, no cookies and no browser. `roomGridData.masterRooms[]`
is the physical-room list; each entry carries id, name, and its OWN image list.
Scraping the DOM instead loses rooms (lazy loading) and bleeds images between
adjacent room cards. This endpoint is the source of truth.

Caveat: the room grid is availability-scoped -- rooms with nothing bookable on
the queried dates are omitted. Probe a date with wide availability.
"""
import datetime
import math
import re
import time
import urllib.parse

import requests

SUGGEST = "https://www.agoda.com/api/cronos/search/GetUnifiedSuggestResult/3/1/1/0/en-us/"
SECONDARY = "https://www.agoda.com/api/cronos/property/BelowFoldParams/GetSecondaryData"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/json",
           "Referer": "https://www.agoda.com/"}
HOTEL_SUGGESTION = 7  # ObjectTypeID for a property (vs city/area/airport)


def session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


class Throttled(Exception):
    """Agoda answered, but not with data."""


# Agoda tolerates a steady trickle and blocks bursts: a 465-hotel run issuing
# ~3 requests per hotel as fast as it could got 502s from hotel 194 onward, and
# the block outlived the run. So requests are paced globally rather than per
# call site, and repeated throttling escalates into a real cooldown instead of
# a tight retry loop that just extends the block.
MIN_INTERVAL = 1.5      # seconds between any two Agoda requests
COOLDOWN = 420          # after CONSECUTIVE_LIMIT straight failures, stand down
COOLDOWN_MAX = 3600     # ceiling for the escalation below
CONSECUTIVE_LIMIT = 6

_last_request = 0.0
_consecutive = 0
# Cooldowns served with NO successful request in between. A flat cooldown that
# resets its counter is what produced a two-hour livelock in production: 6
# failures -> sleep 420s -> reset -> 6 failures -> sleep 420s, forever, at ~2
# hotels per cycle, every one of them recorded as "no agoda match". The block
# was longer than the cooldown, so a fixed cooldown could never outlast it.
_cooldowns = 0


def health():
    """(consecutive_cooldowns, next_cooldown_s). 0 means Agoda is answering.

    Read by the pipeline's circuit breaker: this module can slow itself down,
    but only the caller can decide that a run has stopped being worth
    continuing, and only the caller can tell an operator.
    """
    return _cooldowns, min(COOLDOWN * 2 ** _cooldowns, COOLDOWN_MAX)


def _pace():
    global _last_request
    wait = MIN_INTERVAL - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()


def _note(ok, log=None):
    """Track consecutive failures; stand down, longer each time, when the host
    is clearly done.

    The cooldown DOUBLES per consecutive cooldown that produced no success,
    because the failure this exists for is a block that outlives a fixed
    cooldown -- against that, a constant wait is not backoff, it is a busy-wait
    with a long sleep in it. One success anywhere resets both counters.
    """
    global _consecutive, _cooldowns
    if ok:
        _consecutive = _cooldowns = 0
        return
    _consecutive += 1
    if _consecutive >= CONSECUTIVE_LIMIT:
        wait = min(COOLDOWN * 2 ** _cooldowns, COOLDOWN_MAX)
        _cooldowns += 1
        msg = (f"  agoda throttled {_consecutive}x in a row -- cooldown "
               f"#{_cooldowns}, standing down {wait}s "
               f"(no successful call since cooldown #1)" if _cooldowns > 1 else
               f"  agoda throttled {_consecutive}x in a row -- cooling down {wait}s")
        (log or print)(msg)
        time.sleep(wait)
        _consecutive = 0


def _get_json(s, url, params=None, timeout=60, tries=5, base_delay=3):
    """GET and decode JSON, retrying on throttling as well as network errors.

    Under load Agoda stops raising and starts answering 200 with an HTML
    interstitial. Retrying only on RequestException therefore records a rate
    limit as "this hotel is not on Agoda" -- a silent, permanent data loss that
    looks exactly like a real negative. Anything that is not decodable JSON is
    treated as transient and backed off exponentially.
    """
    last = None
    for i in range(tries):
        _pace()
        try:
            r = s.get(url, params=params, timeout=timeout)
            if r.status_code == 200 and "json" in (r.headers.get("content-type") or ""):
                _note(True)
                return r.json()
            last = Throttled(f"HTTP {r.status_code} "
                             f"{(r.headers.get('content-type') or '?')[:40]}")
        except requests.RequestException as e:
            last = e
        except ValueError as e:                      # 200 but body is not JSON
            last = Throttled(str(e))
        time.sleep(base_delay * (2 ** i))
    _note(False)
    raise last


def suggest(s, name, timeout=25):
    """Resolve a free-text hotel name to Agoda property candidates.

    SuggestionList holds (ObjectID, Name); ViewModelList holds the city/country
    for the same ordinal. Joining them lets us reject same-brand hotels in the
    wrong city ("Hyatt Regency Vancouver") before spending a room fetch.
    """
    url = SUGGEST + "?searchText=" + urllib.parse.quote(name)
    d = _get_json(s, url, timeout=timeout)
    vms = {v["ObjectId"]: v for v in d.get("ViewModelList") or [] if v.get("ObjectId")}
    out = []
    for sug in d.get("SuggestionList") or []:
        if sug.get("ObjectTypeID") != HOTEL_SUGGESTION:
            continue
        vm = vms.get(sug["ObjectID"], {})
        out.append({"agoda_id": sug["ObjectID"], "agoda_name": sug["Name"],
                    "city_id": vm.get("CityId"), "country_id": vm.get("CountryId"),
                    # readable city name + ISO country, when Agoda's index has
                    # a match -- lets a caller with nothing but mangled slug
                    # text recover a real destination name from Agoda's side
                    # even when Bookme's own suggestion index draws a blank
                    "city_name": vm.get("CityName"),
                    "country_iso": (vm.get("CountryISO") or "").lower() or None})
    return out


CITY_SUGGESTION = 5  # ObjectTypeID for a city


def resolve_destination(s, destination, timeout=25):
    """Destination name -> (city_id, country_iso), with NO expected country to
    validate against -- unlike city_id() below, which needs one. Needed when
    the caller reconstructed a destination name from a Bookme slug and has no
    independent idea what country it's in either; this just reports Agoda's
    own answer for that city so a caller can go find out.
    """
    url = SUGGEST + "?searchText=" + urllib.parse.quote(destination)
    d = _get_json(s, url, timeout=timeout)
    vms = {v["ObjectId"]: v for v in d.get("ViewModelList") or [] if v.get("ObjectId")}
    for sug in d.get("SuggestionList") or []:
        if sug.get("ObjectTypeID") == CITY_SUGGESTION:
            cid = sug["ObjectID"]
            return cid, (vms.get(cid, {}).get("CountryISO") or "").lower() or None
    for vm in d.get("ViewModelList") or []:
        if vm.get("CityId"):
            return vm["CityId"], (vm.get("CountryISO") or "").lower() or None
    return None, None


def city_id(s, destination, timeout=25, country_code=None):
    """Agoda's numeric id for a destination name, e.g. "Dubai" -> 2994.

    Derived rather than hardcoded so the pipeline moves to another city by
    changing one string. A name can be shared by cities in different countries
    (a "Dubai" also exists as a minor place name elsewhere) -- ViewModelList
    carries CountryISO for the same ObjectId for free, so the first
    same-country match wins rather than just the first match.
    """
    from . import config
    country_code = (country_code or config.COUNTRY_CODE).lower()
    url = SUGGEST + "?searchText=" + urllib.parse.quote(destination)
    d = _get_json(s, url, timeout=timeout)
    vms = {v["ObjectId"]: v for v in d.get("ViewModelList") or [] if v.get("ObjectId")}

    candidates = [sug["ObjectID"] for sug in d.get("SuggestionList") or []
                  if sug.get("ObjectTypeID") == CITY_SUGGESTION]
    for cid in candidates:
        iso = (vms.get(cid, {}).get("CountryISO") or "").lower()
        if iso == country_code:
            return cid
    if candidates:
        return candidates[0]        # no ISO on record either way; best guess
    for vm in d.get("ViewModelList") or []:      # fall back to the city of the
        if vm.get("CityId"):                     # first property suggestion
            return vm["CityId"]
    return None


# Confirmed live (not the path a hotel description suggested -- that path,
# amenities[].featured, does not exist; `amenities` is a flat facility list
# with no such key). The real location is a `features[]` entry whose title
# starts with "Room size:", e.g. "Room size: 40 m²/431 ft²" -- and NOT
# reliably at a fixed index: confirmed present at index 0 on some properties,
# and features can be absent altogether, so every entry is searched rather
# than assuming a position (the same array-position mistake found and fixed
# twice elsewhere in this project -- tier resolution and view_of()).
#
# Measured across 3 real, structurally different properties this session (a
# standard city hotel, an aparthotel, a business hotel): 15/15 rooms carried
# this feature, and the VALUE varies correctly with room tier on multi-type
# hotels (30/45/50/80 m² across a 6-room aparthotel, scaling with bedroom
# count) -- so this is genuine per-room data, not a hotel-level constant
# that happens to repeat.
_SIZE_RE = re.compile(r"room size:\s*[\d.,]+\s*m[²2]\s*/\s*([\d.,]+)\s*ft[²2]",
                      re.IGNORECASE)


def _size_sqft(m):
    """Square footage from a masterRoom's `features` list, or None.

    Square feet specifically, not square metres: the site's own display
    already carries both units in one string, and storing only one avoids a
    second, purely-derived column -- square metres can be computed from this
    one, in code, whenever something actually needs it.
    """
    for f in m.get("features") or []:
        match = _SIZE_RE.search((f or {}).get("title") or "")
        if match:
            try:
                return round(float(match.group(1).replace(",", "")))
            except ValueError:
                continue
    return None


def master_room(m):
    """One entry of roomGridData.masterRooms -> our room record.

    Shared by the HTTP path and the browser fallback so both emit an identical
    schema; a fallback with its own parser is a second thing to keep in sync.
    """
    return {
        "agoda_room_id": m.get("id"),
        "room_name": (m.get("name") or "").strip(),
        "max_occupancy": m.get("maxOccupancy"),
        "beds": m.get("numberOfBeds"),
        "rate_plan_count": len(m.get("rooms") or []),
        "images": [_abs(u) for u in (m.get("images") or [])],
        "image_captions": m.get("captions") or [],
        "thumbnail": _abs((m.get("roomThumbnail") or {}).get("src") or ""),
        "size_sqft": _size_sqft(m),
    }


_SLUG_RE = re.compile(r"^/([^/]+)/hotel/[^/]+\.html")


def _own_slug(payload):
    """Agoda's own URL slug for the property, harvested from its own response.

    Deriving a slug from the hotel name is unreliable: Agoda disambiguates
    near-identical names with numeric suffixes nothing can guess
    ("landmark-plaza-baniyas_9", "al-bandar-rotana-dubai-creek_2"), and a wrong
    slug silently redirects to the city page or to the property next door.
    """
    stack = [payload]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            stack.extend(o.values())
        elif isinstance(o, list):
            stack.extend(o)
        elif isinstance(o, str):
            m = _SLUG_RE.match(o)
            if m:
                return m.group(1)
    return None


def property_rooms(s, agoda_id, check_in, check_out, timeout=60, adults=None):
    """Full room grid + address + coords for one property. None if unavailable.

    `adults` defaults to config.ADULTS rather than a literal, so the occupancy
    the pipeline settled on applies here too and escalation can vary it.
    """
    from . import config
    adults = config.ADULTS if adults is None else adults
    los = max(1, (datetime.date.fromisoformat(check_out)
                  - datetime.date.fromisoformat(check_in)).days)
    params = {"checkIn": check_in, "checkOut": check_out, "los": los, "rooms": 1,
              "adults": adults, "hotel_id": agoda_id, "all": "true",
              "price_view": 0, "pagetypeid": 7}
    d = _get_json(s, SECONDARY, params=params, timeout=timeout)

    info = d.get("hotelInfo") or {}
    addr = info.get("address") or {}
    latlng = ((d.get("mapParams") or {}).get("latlng") or [None, None])

    grid = d.get("roomGridData") or {}
    rooms = [master_room(m) for m in (grid.get("masterRooms") or [])]

    return {
        "agoda_id": agoda_id,
        # Agoda's own count of suppliers that answered for this property/date.
        # It is the difference between "we were stopped" and "nobody had
        # inventory": an empty grid with supplier_count == 0 is Agoda
        # affirmatively reporting that it asked and got nothing, while an
        # empty grid with supplier_count > 0 is anomalous and worth a retry.
        # Verified live: a healthy property returned 5 rooms / count 2 in the
        # same session where two others returned 0 rooms / count 0, so a zero
        # here is a property-level fact, not a session-wide block. (An actual
        # block does not reach this line -- it arrives as non-JSON and is
        # raised as Throttled by _get_json.)
        "supplier_count": grid.get("supplierCount"),
        "agoda_name": info.get("name") or info.get("englishName"),
        "slug": _own_slug(d),
        "address": addr.get("full", ""),
        "city_id": addr.get("cityId"),
        "lat": latlng[0], "lon": latlng[1],
        # isNHA ("Non-Hotel Accommodation") marks an individually-listed
        # vacation rental rather than the hotel itself. These can share a
        # building -- and therefore coordinates -- with the real hotel, and
        # their listing titles often embed the hotel's name verbatim
        # ("Luxury Burj View...Kempinski Central Avenue..."), so neither name
        # score nor distance alone rules them out; the flag has to.
        "is_nha": bool(info.get("isNHA")),
        "accommodation_type": info.get("accommodationType"),
        "rooms": rooms,
    }


def _abs(url):
    return "https:" + url if url.startswith("//") else url


def km(lat1, lon1, lat2, lon2):
    """Great-circle distance. Used to prove a name match is the same building."""
    if None in (lat1, lon1, lat2, lon2):
        return None
    p = math.pi / 180
    a = (0.5 - math.cos((lat2 - lat1) * p) / 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * (1 - math.cos((lon2 - lon1) * p)) / 2)
    return 12742 * math.asin(math.sqrt(a))


if __name__ == "__main__":
    # Offline first: the exact shapes confirmed live, plus the ones NOT yet
    # seen live but plausible (plain "2" instead of "²", a villa's 4-digit
    # comma-thousands size, size missing) -- these can't wait for a specific
    # property to happen to exhibit them.
    assert _size_sqft({"features": [{"title": "Room size: 40 m²/431 ft²"}]}) == 431
    assert _size_sqft({"features": [{"title": "City view"},           # not index 0
                                    {"title": "Room size: 25 m²/269 ft²"}]}) == 269
    assert _size_sqft({"features": [{"title": "Room size: 30 m2/323 ft2"}]}) == 323
    assert _size_sqft({"features": [{"title": "Room size: 111.5 m²/1,200 ft²"}]}) == 1200
    assert _size_sqft({"features": [{"title": "City view"}]}) is None
    assert _size_sqft({"features": []}) is None
    assert _size_sqft({}) is None
    assert _size_sqft({"features": [{}]}) is None    # feature dict missing "title"
    print("OK: size_sqft parses every observed and plausible feature shape")

    # Asserts the CONTRACT, not today's inventory. No property id, room name or
    # coordinate is pinned, so this keeps passing as Agoda's data changes and
    # still fails if the endpoint's shape does. Takes any destination.
    import datetime
    import sys

    from . import config

    dest = sys.argv[1] if len(sys.argv) > 1 else config.DESTINATION
    s = session()

    cid = city_id(s, dest)
    assert isinstance(cid, int), f"no city id for {dest!r}"

    from .run import weekend_checkin  # same probe night the pipeline uses
    ci = weekend_checkin()
    co = ci + datetime.timedelta(days=config.STAY_NIGHTS)

    # Any single hotel may legitimately have no availability, so walk the
    # suggestions until one has a populated grid rather than pinning one hotel.
    checked = 0
    for c in suggest(s, f"{dest} hotel"):
        p = property_rooms(s, c["agoda_id"], ci.isoformat(), co.isoformat())
        checked += 1
        if not p or not p["rooms"]:
            continue
        assert p["lat"] is not None and p["lon"] is not None, "no coordinates"
        assert p["city_id"] == cid, f"{p['city_id']} != {cid} for a {dest} hotel"
        ids = [r["agoda_room_id"] for r in p["rooms"]]
        assert all(ids), "a room came back without an id"
        assert len(ids) == len(set(ids)), "duplicate room ids in one property"
        assert all(r["room_name"] for r in p["rooms"]), "unnamed room"
        assert all(r["images"] for r in p["rooms"]), "room with no images"
        assert all("size_sqft" in r for r in p["rooms"]), "size_sqft key missing"
        assert all(r["size_sqft"] is None or isinstance(r["size_sqft"], int)
                  for r in p["rooms"]), "size_sqft not an int or None"
        assert km(p["lat"], p["lon"], p["lat"], p["lon"]) < 1e-9
        with_size = sum(1 for r in p["rooms"] if r["size_sqft"] is not None)
        print(f"OK ({dest}, city {cid}): {p['agoda_name']} @ {p['lat']},{p['lon']} "
              f"-- {with_size}/{len(p['rooms'])} rooms carry a size")
        for r in p["rooms"]:
            print(f"  {r['agoda_room_id']:>12}  {r['room_name'][:38]:40} "
                  f"{len(r['images']):>2} imgs, {r['rate_plan_count']} rate plans, "
                  f"{r['size_sqft']} sqft")
        break
    else:
        raise AssertionError(f"no property with rooms among {checked} suggestions")
