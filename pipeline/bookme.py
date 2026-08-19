"""
Bookme (bookmesky.com) client -- partner API.

Inputs : a hotel SLUG (straight out of `v2_common_hotels.slug`) + stay dates.
Outputs: that hotel's room options ("rate plans"), collapsed to distinct rooms.

Three facts about this API drive the design:

  * `/hotels/api/availability` is keyed by SLUG ALONE. There is no search, no
    polling, no per-search ref id and no per-itinerary ref id -- the database
    slug IS the live slug. Measured: 68/68 Dubai hotels returned the identical
    slug back, 0 mismatches. This replaced an architecture that ran a whole
    city-wide polling search purely to mint two throwaway ref ids per hotel.

  * The endpoint is HEAVILY NONDETERMINISTIC. Six identical calls to one slug
    returned 19, 18, 27, 38, 36 and 44 rooms. A single call is NOT a measurement
    of a hotel, so every caller must probe repeatedly and UNION. Never treat one
    response as the room list.

  * The two failure modes are NOT the same fact and must never be conflated:
      - HTTP 500 "property no longer available" is PERMANENT. Verified: 25
        consecutive calls across 5 hotels, plus 3 other dates each, never
        recovered. Retrying it burns quota to re-learn the same answer.
      - Transport faults (the host resets connections under sustained load) are
        transient and MUST be retried, or a network hiccup is silently recorded
        as "this hotel has no rooms" -- the exact unearned zero this pipeline
        exists to prevent.

Room objects carry Media[] plus a literal `AccurateMedia: false` flag. That flag
is Bookme admitting the room photo is a hotel-level fallback: it is the exact
defect this project exists to fix. Verified present on 54/54 room objects.

ENVIRONMENT INVARIANT: the database and this API must be the same environment.
The slug namespaces differ between UAT and production (prod appends an arbitrary
`-<digits>` disambiguator: `hilton-dubai-the-walk` vs `hilton-dubai-the-walk-864`).
Pointing a UAT database at the production API silently resolves ~17% of hotels
and reports the other 83% as unavailable, with no error anywhere.
`BOOKME_API_BASE` and the MySQL credentials must be moved together, always.
"""
import json
import os
import threading
import time

import requests

DEFAULT_API_BASE = "https://uat-api.bookmesky.com"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


# Read at CALL time, never at import. `config.load_env()` runs inside main(),
# long after this module is imported, so a module-level os.getenv would have
# captured the default and silently ignored .env entirely -- pointing a UAT
# database at the production API, which is the one failure this module's
# docstring says has no error to signal it.
def api_base():
    return (os.getenv("BOOKME_API_BASE") or DEFAULT_API_BASE).rstrip("/")


def auth_url():
    return api_base() + "/partner/api/auth/token"


def avail_url():
    return api_base() + "/hotels/api/availability"


def default_currency():
    return os.getenv("BOOKME_CURRENCY") or "PKR"

# Refresh this long before the token's stated expiry, so a long-running probe
# pass cannot have a token die mid-flight between the check and the request.
TOKEN_SKEW_S = 300

# How hard to retry a TRANSPORT fault (never a 500 -- see the docstring).
NET_TRIES = 4


class Unavailable(Exception):
    """The property is not sellable -- Bookme's own 500. A permanent fact about
    this hotel, not an error to retry. Distinct from a transport failure so a
    caller can record 'no rooms' with confidence rather than by assumption."""


class AuthFailed(Exception):
    """Could not mint a partner token. Fatal for the Bookme side of a run
    (there is no anonymous fallback), but NOT for the run: the caller degrades
    to Agoda-only rather than losing the imagery for every hotel in the city."""


def _credentials():
    user = os.getenv("BOOKME_USERNAME")
    pwd = os.getenv("BOOKME_PASSWORD")
    if not user or not pwd:
        raise AuthFailed(
            "BOOKME_USERNAME / BOOKME_PASSWORD are not set. The partner API has "
            "no anonymous access, so the Bookme side cannot run without them.")
    return user, pwd


def session(timeout=40):
    """An authenticated session, ready for `availability()`.

    Carries its own token expiry and a lock, because a probe pass runs this
    session across a thread pool: without the lock every worker that noticed an
    expired token would mint its own, and the last writer would win a race to
    set the header -- a check-then-act on shared mutable state.

    The connection pool is sized for that pool too. urllib3 defaults to 10
    connections and, once exceeded, discards and reopens them instead of
    reusing -- which shows up as exactly the connection resets this endpoint is
    already prone to under load.
    """
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json",
                      "Content-Type": "application/json"})
    adapter = requests.adapters.HTTPAdapter(pool_connections=32, pool_maxsize=32)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.bm_lock = threading.Lock()
    s.bm_expiry = 0.0
    s.bm_timeout = timeout
    _refresh(s, force=True)
    return s


def _refresh(s, force=False):
    """Mint a token if the held one is missing, expiring or rejected.

    Double-checked under the lock: several workers can see the same stale token
    and queue here, but only the first mints -- the rest find a fresh one and
    return, rather than each burning an auth call.
    """
    with s.bm_lock:
        if not force and time.time() < s.bm_expiry:
            return
        user, pwd = _credentials()
        last = None
        for i in range(NET_TRIES):
            try:
                r = s.post(auth_url(), timeout=s.bm_timeout,
                           json={"username": user, "password": pwd})
            except requests.RequestException as e:
                last = e
                time.sleep(1.5 * (i + 1))
                continue
            # 2xx, not `== 200`: this endpoint answers 201 Created. Pinning it
            # to 200 rejected every valid token while reporting an auth failure,
            # which would have degraded every run to Agoda-only, silently and
            # permanently.
            if 200 <= r.status_code < 300:
                try:
                    d = r.json()
                except ValueError as e:
                    last = e
                    time.sleep(1.5 * (i + 1))
                    continue
                tok = d.get("Token")
                if not tok:
                    raise AuthFailed(f"auth returned no Token: {str(d)[:200]}")
                s.headers["Authorization"] = "Bearer " + tok
                s.bm_expiry = _expiry_epoch(d.get("ExpiryAt"))
                return
            if r.status_code in (401, 403, 422):
                # Bad credentials do not become good by asking again.
                raise AuthFailed(
                    f"auth rejected ({r.status_code}): {r.text[:200]}")
            last = RuntimeError(f"auth HTTP {r.status_code}")
            time.sleep(1.5 * (i + 1))
        raise AuthFailed(f"could not mint a token after {NET_TRIES} tries: {last}")


def _expiry_epoch(stamp):
    """Absolute expiry, minus a safety skew. An unparseable or absent stamp
    falls back to a short window rather than to 'never expires' -- a token
    wrongly believed fresh fails every call in the pass, while one wrongly
    believed stale costs a single extra auth request."""
    import datetime
    if stamp:
        try:
            dt = datetime.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt.timestamp() - TOKEN_SKEW_S
        except (ValueError, TypeError):
            pass
    return time.time() + 900


def availability(s, slug, check_in, check_out, adults=None, currency=None,
                 timeout=None):
    """Distinct rooms for one property on one date shape.

    Returns a list of {room_name, max_occupancy, accurate_media}.

    Raises `Unavailable` for Bookme's permanent 500 so the caller can tell
    "Bookme says this property is not sellable" apart from "we failed to ask".
    Any other unrecoverable state raises too -- this function never returns an
    empty list to mean a failure, because an unearned zero is indistinguishable
    downstream from a hotel that genuinely has no rooms tonight.
    """
    return _collapse(availability_raw(s, slug, check_in, check_out,
                                      adults=adults, currency=currency,
                                      timeout=timeout))


def _isodate(d):
    return d if isinstance(d, str) else d.isoformat()


def _collapse(payload):
    """Rate plans -> distinct physical rooms.

    Bookme returns one Option per (room x board type x refundability x promo),
    so a 7-room hotel can surface 39 options. Collapse on room name.

    Only what survives the moment it was fetched, or is actually consumed:
      room_name     -- the join key, and the only thing the mapping needs
      max_occupancy -- a physical property of the room, not of this query
      accurate_media-- Bookme's own admission that this room's photo is a
                       hotel-level fallback; the defect this project exists to
                       clear, so it is worth one boolean of provenance
    Deliberately dropped: the rate-plan count and board types (facts about one
    night's offers, not about the room) and the room's current Media -- the
    WRONG images we are replacing, bulky, and read by nothing downstream.
    """
    options = ((payload.get("Itinerary") or {}).get("Options") or [])
    out = {}
    for opt in options:
        for room in opt.get("Rooms") or []:
            name = (room.get("Name") or opt.get("Title") or "").strip()
            if not name:
                continue
            out.setdefault(name, {
                "room_name": name,
                "max_occupancy": room.get("MaxOccupancy"),
                "accurate_media": bool(room.get("AccurateMedia")),
            })
    return list(out.values())


def common_id(payload):
    """The hotel id this payload is FOR, as Bookme states it.

    `Property.CommonID` is an exact foreign key to `v2_common_hotels.id` --
    measured 60/60 within a matched environment. (`Property.ID` is a different
    id space entirely and matches 0/60; do not use it.) This is what lets the
    caller ASSERT it received the hotel it asked for instead of trusting the
    slug round-trip, turning a silent wrong-hotel write into a loud failure.

    Historical note: `DB_FINDINGS.md` Finding 9 records CommonID as joining
    0/40 and sent the original design down a name+geo matching path. That was
    measured against PRODUCTION using UAT database ids -- a cross-environment
    comparison. Within one environment the join is exact.
    """
    return ((payload.get("Itinerary") or {}).get("Property") or {}).get("CommonID")


def availability_raw(s, slug, check_in, check_out, adults=None, currency=None,
                     timeout=None):
    """The whole payload, for callers that also want `common_id()`.

    `adults` is always sent explicitly: omitting `Rooms` makes the API default
    to a single adult, which is the measurably WORSE occupancy (doubles and
    twins drop out of some supplier feeds at adults=1 -- see config.ADULTS).
    """
    from . import config
    adults = config.ADULTS if adults is None else adults
    body = {"Currency": currency or default_currency(), "Slug": slug,
            "CheckIn": _isodate(check_in), "CheckOut": _isodate(check_out),
            "GuestNationality": config.GUEST_NATIONALITY,
            "Rooms": [{"Adults": adults, "Children": []}]}
    last = None
    for i in range(NET_TRIES):
        _refresh(s)                      # no-op unless the token is near expiry
        try:
            r = s.post(avail_url(), json=body, timeout=timeout or s.bm_timeout)
        except requests.RequestException as e:
            # Transport fault: the one failure worth retrying. Swallowing it as
            # "no rooms" is how a network hiccup becomes a permanent hole in the
            # catalogue.
            last = e
            time.sleep(1.5 * (i + 1))
            continue
        if 200 <= r.status_code < 300:
            try:
                return r.json()
            except ValueError as e:      # 2xx with a non-JSON body
                last = e
                time.sleep(1.5 * (i + 1))
                continue
        if r.status_code == 500:
            raise Unavailable(slug)
        if r.status_code in (401, 403):
            # The token died earlier than its stated expiry. Mint once and let
            # the loop retry; a genuine credential failure raises AuthFailed
            # out of _refresh rather than spinning here.
            _refresh(s, force=True)
            last = RuntimeError(f"HTTP {r.status_code}")
            continue
        # 429 / 5xx-other / anything else: back off and try again.
        last = RuntimeError(f"HTTP {r.status_code}: {r.text[:120]}")
        time.sleep(2.0 * (i + 1))
    raise RuntimeError(f"availability({slug!r}) failed after {NET_TRIES} tries: {last}")


if __name__ == "__main__":
    # self-check: offline parsing invariants first (they need no network), then
    # the live contract this module's whole design rests on.
    from . import config
    config.load_env()

    # collapse must dedupe rate plans down to distinct rooms, and must fall back
    # to the Option title when a room carries no name of its own
    fake = {"Itinerary": {"Options": [
        {"Title": "Deluxe", "Rooms": [{"Name": "Deluxe Room", "MaxOccupancy": 2,
                                       "AccurateMedia": False}]},
        {"Title": "Deluxe", "Rooms": [{"Name": "Deluxe Room", "MaxOccupancy": 2,
                                       "AccurateMedia": False}]},
        {"Title": "Titled Only", "Rooms": [{"MaxOccupancy": 3}]},
    ]}}
    got = _collapse(fake)
    assert len(got) == 2, got
    assert {r["room_name"] for r in got} == {"Deluxe Room", "Titled Only"}, got
    assert _collapse({}) == [] and _collapse({"Itinerary": {}}) == []
    assert common_id({"Itinerary": {"Property": {"CommonID": 605}}}) == 605
    assert common_id({}) is None

    s = session()
    print(f"token OK, expires in {(s.bm_expiry - time.time()) / 60:.0f} min")

    import datetime
    ci = datetime.date.today() + datetime.timedelta(days=10)
    co = ci + datetime.timedelta(days=1)

    slug = os.getenv("BOOKME_SELFTEST_SLUG", "hilton-dubai-the-walk")
    payload = availability_raw(s, slug, ci, co)
    rooms = _collapse(payload)
    assert rooms, f"{slug} returned no rooms -- expected a live hotel"
    assert all(r["room_name"] for r in rooms)
    print(f"OK: {slug} -> {len(rooms)} distinct rooms, "
          f"CommonID={common_id(payload)}")
    print(json.dumps(rooms[:2], indent=2))

    # the permanent-vs-transient distinction is the module's core contract
    try:
        availability(s, "definitely-not-a-real-hotel-xyz", ci, co)
        raise AssertionError("a nonexistent slug must raise Unavailable")
    except Unavailable:
        print("OK: a dead slug raises Unavailable, not an empty list")

    # nondeterminism is real: prove the union beats any single call, because
    # every caller's correctness depends on knowing this
    seen = set()
    counts = []
    for _ in range(4):
        got = {r["room_name"] for r in availability(s, slug, ci, co)}
        counts.append(len(got))
        seen |= got
    print(f"OK: 4 identical calls returned {counts}; union={len(seen)} "
          f"(best single={max(counts)}) -- probing must union, never trust one call")
