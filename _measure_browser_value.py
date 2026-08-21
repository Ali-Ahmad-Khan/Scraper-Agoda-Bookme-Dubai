"""O-5: is the Agoda browser fallback still worth its cost?

The module docstring claims it rescues "roughly a third" of HTTP-empty
properties. Two production runs measured ~10% instead (8/89, 3/26).

But rescue rate alone overstates the question, because agoda_rooms() unions
the browser's result INTO the escalation ladder (`_escalate(..., already=
browser_rooms)`), and the ladder runs regardless of whether the browser found
anything. The real question is MARGINAL: does the browser find rooms the
ladder does not independently find anyway?

This measures, for properties whose BASE HTTP call returns empty:
  (a) browser-only room count
  (b) ladder-only room count (browser skipped entirely)
  (c) union (what a real run actually publishes)
  (d) the browser's MARGINAL contribution: rooms in (c) not already in (b)

Live, read-only. Touches Agoda and the browser; never the database.
"""
import sys
import time

from pipeline import agoda, agoda_browser as ab, config, db
from pipeline import run as pl

config.load_env()


def main(n=10):
    conn = db.connect()
    hotels = [h for h in db.hotels(conn, [1280], limit=200) if h["lat"] is not None]
    conn.close()

    ags = agoda.session()
    ci, co = pl.stay()

    # Find HTTP-empty properties first -- the population this fallback exists
    # for. Cheap: one base-date call per hotel, no ladder yet.
    empty = []
    for h in hotels:
        if len(empty) >= n:
            break
        m = pl.match_hotel(ags, h, ci, co, city=2994, destination="Dubai",
                           country_name="United Arab Emirates")
        if not m.get("agoda_id"):
            continue
        p = agoda.property_rooms(ags, m["agoda_id"], ci, co)
        if p and not p["rooms"]:
            empty.append({"hotel": h, "match": m})
    print(f"found {len(empty)} HTTP-empty properties to test\n")
    if not empty:
        print("nothing to measure -- no HTTP-empty properties in this sample")
        return

    print(f"{'hotel':30} {'browser':>8} {'ladder':>7} {'union':>6} "
          f"{'marginal':>9} {'browser_s':>10}")
    tot_browser = tot_ladder = tot_union = tot_marginal = 0
    tot_browser_s = 0.0
    for e in empty:
        h, m = e["hotel"], e["match"]
        hid = m["agoda_id"]
        target = {"agoda_id": hid, "agoda_name": m["agoda_name"],
                 "slug": m.get("slug"), "city": "Dubai"}

        t0 = time.monotonic()
        got = ab.fetch_rooms_sync([target], ci, co,
                                  country_code="ae", log=lambda *_: None)
        browser_s = time.monotonic() - t0
        browser_rooms = got.get(hid) or []

        ladder_rooms = pl._escalate(ags, hid, m["agoda_name"], already=[])
        union_rooms = pl._escalate(ags, hid, m["agoda_name"], already=browser_rooms)

        ladder_ids = {r["agoda_room_id"] for r in ladder_rooms}
        union_ids = {r["agoda_room_id"] for r in union_rooms}
        marginal = len(union_ids - ladder_ids)   # what the browser ADDED beyond the ladder

        tot_browser += len(browser_rooms)
        tot_ladder += len(ladder_rooms)
        tot_union += len(union_rooms)
        tot_marginal += marginal
        tot_browser_s += browser_s

        flag = "  <-- browser found something the ladder alone missed" if marginal else ""
        print(f"{(h['name'] or '')[:29]:30} {len(browser_rooms):>8} "
              f"{len(ladder_rooms):>7} {len(union_rooms):>6} {marginal:>9} "
              f"{browser_s:>9.1f}s{flag}")

    print(f"\ntotals over {len(empty)} HTTP-empty properties:")
    print(f"  browser alone : {tot_browser} rooms")
    print(f"  ladder alone  : {tot_ladder} rooms")
    print(f"  union         : {tot_union} rooms")
    print(f"  MARGINAL (browser beyond what the ladder finds on its own): "
          f"{tot_marginal} rooms")
    print(f"  browser time spent: {tot_browser_s:.0f}s "
          f"({tot_browser_s/len(empty):.1f}s/property)")
    if tot_marginal == 0:
        print(f"\nVERDICT: the ladder alone recovered everything the browser did, "
              f"across all {len(empty)} properties. The browser's entire measured "
              f"contribution here was REDUNDANT with work already being done.")
    else:
        print(f"\nVERDICT: browser added {tot_marginal} room(s) the ladder did not "
              f"find on its own, at a cost of {tot_browser_s:.0f}s "
              f"({tot_browser_s/max(tot_marginal,1):.0f}s per marginal room).")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 10)
