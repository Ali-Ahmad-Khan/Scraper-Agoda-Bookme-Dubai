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
import json
import re

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


async def fetch_rooms(targets, check_in, check_out, country_code=None,
                      log=print, settle_ms=2500, scrolls=6):
    """targets: [{agoda_id, agoda_name, city}]  ->  {agoda_id: [room, ...]}"""
    from . import config
    country_code = country_code or config.COUNTRY_CODE
    out = {}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=agoda.UA, viewport={"width": 1400, "height": 1000},
            locale="en-US")
        page = await ctx.new_page()
        captured = {}
        # Response bodies MUST be awaited before the page navigates away or the
        # browser closes -- Playwright discards them, and the read then fails
        # with TargetClosedError. The handler is fire-and-forget (page.on
        # cannot await), so its tasks are tracked here and drained explicitly
        # after each visit. Without the drain, `captured` was read while every
        # body was still pending and this whole fallback silently returned
        # zero rooms for EVERY property -- a total, silent failure that looked
        # exactly like "this hotel has no rooms".
        pending = []

        async def on_response(r):
            if "GetSecondaryData" not in r.url:
                return
            try:
                body = json.loads(await r.text())
            except Exception:
                return
            grid = (body.get("roomGridData") or {})
            hid = _hotel_id(r.url)
            if hid is not None:
                captured[hid] = grid.get("masterRooms") or []

        page.on("response", lambda r: pending.append(asyncio.create_task(on_response(r))))

        async def drain():
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
                pending.clear()

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

        await browser.close()
    return out


async def _visit(page, url, scrolls, settle_ms):
    """Load and scroll a page. A redirect mid-scroll destroys the execution
    context; that is a normal Agoda behaviour, not a reason to abandon the
    hotel, so it is absorbed rather than raised."""
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

    got = asyncio.run(fetch_rooms([control], ci.isoformat(), co.isoformat(),
                                  country_code=config.COUNTRY_CODE))
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
