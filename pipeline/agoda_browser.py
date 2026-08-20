"""
Browser fallback for Agoda properties the HTTP path cannot see.

Input : Agoda property ids whose JSON room grid came back empty.
Output: the same room dicts agoda.property_rooms() produces.

Why this exists: for roughly a third of Dubai properties, GetSecondaryData
returns `supplierCount: 0` and no rooms to an ordinary HTTP client, while a real
browser hitting the same endpoint, same property, same dates, in the same
second, gets a populated grid (observed on One&Only One Za'abeel: 0 vs 14
rooms -- one example of the class, not a special case). Loading
the property page in a session, replaying its cookies, its sessionid, its
psePageSessionId and its exact query string all failed to reproduce it, so the
gap is something the page's JavaScript computes. Rather than fight that, the
pipeline runs the cheap HTTP path for everyone and pays for a browser only on
the properties that provably need one.

The browser is opened ONCE for the whole batch and navigated between properties.
Relaunching per hotel is both slow and exactly the pattern bot-detection looks
for.

We do not scrape the DOM here -- we let the page make its own API call and read
the JSON response off the wire, so the parsing stays identical to the HTTP path
and stays immune to Agoda's obfuscated class names.
"""
import asyncio
import atexit
import json
import re
import threading

from playwright.async_api import async_playwright

from . import agoda


def property_url(slug, city, country_code):
    place = re.sub(r"[^a-z0-9]+", "-", (city or "").lower()).strip("-")
    return f"https://www.agoda.com/{slug}/hotel/{place}-{country_code.lower()}.html"


def slug_candidates(target):
    """URL slugs to try for one property, best first.

    Agoda's OWN slug (harvested from its API response) is authoritative: it
    carries the numeric suffixes that disambiguate near-identical properties
    ("landmark-plaza-baniyas_9"). A slug derived from the display name is only a
    fallback, and a bad one does not 404 -- it silently redirects to the city
    page or to a neighbouring hotel, which is why the landing is verified.
    """
    out = []
    if target.get("slug"):
        out.append(target["slug"])
    derived = re.sub(r"[^a-z0-9]+", "-", (target.get("agoda_name") or "").lower()).strip("-")
    if derived and derived not in out:
        out.append(derived)
    return out


# ------------------------------------------------- the persistent browser
# ONE Chrome for the whole run, not one per hotel.
#
# The module docstring above has always said the browser should be "opened ONCE
# for the whole batch" because "relaunching per hotel is both slow and exactly
# the pattern bot-detection looks for" -- but `fetch_rooms` launched and closed
# its own browser on every call, and run.py calls it with ONE hotel at a time.
# The first production run therefore launched and tore down Chrome 89 separate
# times. The module was describing its own worst behaviour.
#
# The justification that used to sit in run.py -- "the price of publishing hotel
# by hotel so a crash cannot lose a day's work" -- expired when discovery and
# commit were split into two phases (D-46). Discovery no longer publishes
# anything, and its progress is check-pointed per hotel to plan.csv, so a
# long-lived browser cannot lose work that the checkpoint is already holding.
#
# Why it matters beyond the ~4s per launch: 89 brand-new Chrome instances with
# empty profiles hitting the same host is a far stronger automation signal than
# one session browsing 89 pages. Continuity is the point, not just the saving.
#
# Playwright objects are bound to the event loop that created them, and the
# pipeline calls this from ordinary sync code. So the loop lives in one daemon
# thread for the process lifetime and every browser operation is submitted to
# it -- rather than `asyncio.run()` per call, which closes the loop and takes
# the browser with it.
_loop = None
_loop_lock = threading.Lock()
_pw = _browser = _ctx = _page = None


def _loop_thread():
    global _loop
    with _loop_lock:
        if _loop is not None:
            return _loop
        loop = asyncio.new_event_loop()
        t = threading.Thread(target=loop.run_forever, name="agoda-browser",
                             daemon=True)
        t.start()
        _loop = loop
        return _loop


def _submit(coro, timeout=180):
    """Run a coroutine on the persistent loop from sync code."""
    return asyncio.run_coroutine_threadsafe(coro, _loop_thread()).result(timeout)


async def _session():
    """The shared page, created on first use. Reused for every later visit."""
    global _pw, _browser, _ctx, _page
    if _page is not None and not _page.is_closed():
        return _page
    _pw = await async_playwright().start()
    _browser = await _pw.chromium.launch(headless=True)
    _ctx = await _browser.new_context(
        user_agent=agoda.UA, viewport={"width": 1400, "height": 1000},
        locale="en-US")
    _page = await _ctx.new_page()
    return _page


async def _teardown():
    global _pw, _browser, _ctx, _page
    for obj, meth in ((_ctx, "close"), (_browser, "close"), (_pw, "stop")):
        try:
            if obj is not None:
                await getattr(obj, meth)()
        except Exception:                                       # noqa: BLE001
            pass
    _pw = _browser = _ctx = _page = None


def close():
    """Drop the browser. Safe to call when one was never opened."""
    if _loop is None or _browser is None:
        return
    try:
        _submit(_teardown(), timeout=30)
    except Exception:                                           # noqa: BLE001
        pass


def rotate():
    """Fresh browser identity, for the run's Agoda circuit breaker. The HTTP
    side rotates its session on a block; without this the browser would keep
    presenting the same one."""
    close()


atexit.register(close)


def fetch_rooms_sync(targets, check_in, check_out, country_code=None,
                     log=print, settle_ms=2500, scrolls=6):
    """Sync entry point -- what the pipeline calls.

    Replaces `asyncio.run(fetch_rooms(...))`, which created and destroyed a
    fresh event loop (and therefore a fresh browser) on every hotel.
    """
    return _submit(fetch_rooms(targets, check_in, check_out,
                               country_code=country_code, log=log,
                               settle_ms=settle_ms, scrolls=scrolls))


async def fetch_rooms(targets, check_in, check_out, country_code=None,
                      log=print, settle_ms=2500, scrolls=6):
    """targets: [{agoda_id, agoda_name, city}]  ->  {agoda_id: [room, ...]}

    Uses the PERSISTENT browser (see `_session`) instead of launching its own.
    The pipeline reaches this through `fetch_rooms_sync`; this stays async so
    the module selftest can drive it directly.
    """
    from . import config
    country_code = country_code or config.COUNTRY_CODE
    out = {}
    page = None
    for attempt in (1, 2):
        try:
            page = await _session()
            break
        except Exception as e:                                  # noqa: BLE001
            if attempt == 2:
                log(f"  browser unavailable ({type(e).__name__}: {e})")
                return {t["agoda_id"]: [] for t in targets}
            await _teardown()          # wedged since last use; rebuild once

    captured = {}
    # Response bodies MUST be awaited before the page navigates away or the
    # browser closes -- Playwright discards them, and the read then fails with
    # TargetClosedError. The handler is fire-and-forget (page.on cannot await),
    # so its tasks are tracked here and drained explicitly after each visit.
    # Without the drain, `captured` was read while every body was still pending
    # and this whole fallback silently returned zero rooms for EVERY property --
    # a total, silent failure that looked exactly like "this hotel has no rooms".
    pending = []

    async def on_response(r):
        if "GetSecondaryData" not in r.url:
            return
        try:
            body = json.loads(await r.text())
        except Exception:                                       # noqa: BLE001
            return
        grid = (body.get("roomGridData") or {})
        hid = _hotel_id(r.url)
        if hid is not None:
            captured[hid] = grid.get("masterRooms") or []

    handler = lambda r: pending.append(asyncio.create_task(on_response(r)))  # noqa: E731
    page.on("response", handler)

    async def drain():
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
            pending.clear()

    try:
        for i, t in enumerate(targets, 1):
            hid = t["agoda_id"]
            rooms, landed = [], None
            for slug in slug_candidates(t):
                captured.pop(hid, None)
                url = (property_url(slug, t.get("city"), country_code)
                       + f"?checkIn={check_in}&checkOut={check_out}"
                         f"&los=1&rooms=1&adults=2")
                if not await _visit(page, url, scrolls, settle_ms):
                    await drain()
                    continue
                await drain()
                # Landing is verified by whether the page asked the API for OUR
                # property id, not by the URL we requested.
                if hid in captured:
                    landed = slug
                    rooms = [agoda.master_room(m) for m in captured[hid]]
                    break

            out[hid] = rooms
            note = "" if landed else "  (no slug reached this property)"
            log(f"  [{i}/{len(targets)}] {str(t.get('agoda_name'))[:38]:40} "
                f"{len(rooms)} rooms{note}")
    finally:
        # The page OUTLIVES this call now, so the listener must come off or
        # every later visit accumulates another copy of it.
        try:
            page.remove_listener("response", handler)
        except Exception:                                       # noqa: BLE001
            pass
        # Per-hotel mode: drop the browser so the next call starts from a clean
        # profile. Which of the two is safer against Agoda is UNMEASURED -- see
        # config.AGODA_BROWSER_PERSIST -- so it is a switch, not a belief.
        if not getattr(config, "AGODA_BROWSER_PERSIST", True):
            await _teardown()
    return out


# A property page load is not one request -- it pulls HTML, JS, XHR and images
# from Agoda in a burst. This pacer charge is what stops the browser fallback
# from being a hole in the rate discipline: in the first production run it made
# 89 full page loads that the HTTP pacer never saw at all, on the same address,
# while the HTTP path was carefully spacing itself at 1.5s. Charged at 8 slots
# as a deliberate under-estimate of a real page load -- the point is that it
# costs something, not that the number is exact.
BROWSER_PACE_COST = 8


async def _visit(page, url, scrolls, settle_ms):
    """Load and scroll a page. A redirect mid-scroll destroys the execution
    context; that is a normal Agoda behaviour, not a reason to abandon the
    hotel, so it is absorbed rather than raised."""
    # Charged against the SAME global pacer the HTTP path uses, before the page
    # is even requested -- otherwise this path is an unmetered burst against a
    # host the rest of the pipeline is carefully pacing itself for. Run in a
    # thread: _pace() sleeps, and sleeping on the event loop would stall the
    # browser's own I/O rather than delaying our request.
    await asyncio.to_thread(agoda._pace, BROWSER_PACE_COST)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception:
        return False
    for _ in range(scrolls):
        try:
            await page.evaluate("window.scrollBy(0, 900)")
        except Exception:
            pass                      # navigated under us; keep waiting for XHRs
        await page.wait_for_timeout(900)
    await page.wait_for_timeout(settle_ms)
    return True


def _hotel_id(url):
    m = re.search(r"[?&]hotel_id=(\d+)", url)
    return int(m.group(1)) if m else None


if __name__ == "__main__":
    # Proves the browser path can actually extract rooms, using a CONTROL
    # property that the HTTP path already sees rooms for.
    #
    # It deliberately does NOT assert on a property the HTTP path sees zero
    # rooms for, which is what this check used to do: "HTTP saw 0" has two
    # indistinguishable causes -- the property is blocked/obfuscated over
    # plain HTTP (the fallback should recover it), or it genuinely has no
    # availability on these dates (0 is the correct answer and the browser
    # returns 0 too). Asserting on that case fails on correct behaviour.
    #
    # A control property with known-good rooms has neither ambiguity, and it
    # is the assertion that catches real breakage: it caught the response
    # bodies never being awaited (TargetClosedError), which had this entire
    # fallback silently returning zero rooms for every property.
    import datetime
    import sys

    from . import config

    dest = sys.argv[1] if len(sys.argv) > 1 else config.DESTINATION
    from .run import weekend_checkin  # same probe night the pipeline uses
    ci = weekend_checkin()
    co = ci + datetime.timedelta(days=config.STAY_NIGHTS)
    s = agoda.session()

    candidates = agoda.suggest(s, f"{dest} hotel")

    control = None
    for c in candidates[:8]:
        p = agoda.property_rooms(s, c["agoda_id"], ci.isoformat(), co.isoformat())
        if p and p["rooms"]:
            # Agoda's OWN slug, passed through exactly as agoda_rooms() in
            # run.py does -- a slug derived from the display name does not
            # always reproduce the real URL, so dropping it tests a weaker
            # path than production.
            control = {"agoda_id": p["agoda_id"], "agoda_name": p["agoda_name"],
                       "slug": p.get("slug"), "city": dest,
                       "http_rooms": len(p["rooms"])}
            break
    if control is None:
        print(f"skip: no {dest} property returned rooms over HTTP, so there is "
              "no control to compare the browser against")
        raise SystemExit(0)

    # fetch_rooms_sync, not asyncio.run: the browser lives on the module's own
    # persistent loop, and a fresh loop here would build a second one.
    got = fetch_rooms_sync([control], ci.isoformat(), co.isoformat(),
                           country_code=config.COUNTRY_CODE)
    rooms = got.get(control["agoda_id"]) or []
    assert rooms, (
        f"browser recovered NOTHING for {control['agoda_name']!r}, which the "
        f"HTTP path sees {control['http_rooms']} rooms for -- the browser "
        f"fallback is broken, not the property")
    assert all(r["agoda_room_id"] for r in rooms), "room without an id"
    assert all(r["images"] for r in rooms), "room without images"
    assert set(rooms[0]) == set(agoda.master_room({})), "schema drift vs HTTP path"
    print(f"OK: {control['agoda_name']} -- HTTP {control['http_rooms']} rooms, "
          f"browser {len(rooms)} rooms")
    for r in rooms[:5]:
        print(f"   {r['agoda_room_id']:>12} {r['room_name'][:44]:46} {len(r['images'])} imgs")
