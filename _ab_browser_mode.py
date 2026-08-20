"""A/B: persistent browser vs fresh-browser-per-hotel, against live Agoda.

Settles a question that has so far only been argued: does reusing one Chrome
across hotels get detected sooner than launching a clean one each time?

Both arms use the SAME IP -- that is not a variable here and never was. The only
difference is whether the browser profile/cookie jar carries over.

Design notes, because a sloppy A/B here would be worse than no A/B:
  * Same property set for both arms, so a hotel that genuinely has no rooms
    cannot make one arm look worse.
  * ORDER IS ALTERNATED per property (ABBA), so a block that builds up over
    time cannot be attributed to whichever arm happened to run second.
  * Both arms go through the real `fetch_rooms`, so both pay the same pacer
    cost and the same slug-verification logic.
  * The metric is ROOMS RECOVERED and VISIT FAILURES, not wall clock -- the
    question is detection, not speed.

Read-only: touches Agoda only, never the database.
"""
import sys
import time

from pipeline import agoda, agoda_browser as ab, config, db
from pipeline import run as pl


def arm(persist, targets, ci, co, iso):
    config.AGODA_BROWSER_PERSIST = persist
    ab.close()                      # every arm starts from a clean slate
    got, t0 = {}, time.monotonic()
    for t in targets:
        r = ab.fetch_rooms_sync([t], ci, co, country_code=iso, log=lambda *_: None)
        got[t["agoda_id"]] = len(r.get(t["agoda_id"]) or [])
    ab.close()
    return got, time.monotonic() - t0


def main(n=8):
    config.load_env()
    conn = db.connect()
    hotels = [h for h in db.hotels(conn, [1280], limit=60) if h["lat"] is not None]
    conn.close()

    ags = agoda.session()
    ci, co = pl.stay()
    targets = []
    for h in hotels:
        if len(targets) >= n:
            break
        m = pl.match_hotel(ags, h, ci, co, city=2994, destination="Dubai",
                           country_name="United Arab Emirates")
        if m.get("agoda_id"):
            targets.append({"agoda_id": m["agoda_id"], "agoda_name": m["agoda_name"],
                            "slug": m.get("slug"), "city": "Dubai"})
    print(f"resolved {len(targets)} live Agoda properties for the test\n")

    # ABBA: persistent first on even properties, per-hotel first on odd ones,
    # so ordering cannot favour either arm.
    first, second = targets[0::2], targets[1::2]
    p1, t_p1 = arm(True, first, ci, co, "ae")
    h1, t_h1 = arm(False, first, ci, co, "ae")
    h2, t_h2 = arm(False, second, ci, co, "ae")
    p2, t_p2 = arm(True, second, ci, co, "ae")

    persist = {**p1, **p2}
    fresh = {**h1, **h2}
    print(f"{'property':>12}  {'persistent':>10}  {'per-hotel':>10}")
    for k in persist:
        flag = "" if persist[k] == fresh.get(k) else "   <-- DIFFERS"
        print(f"{k:>12}  {persist[k]:>10}  {fresh.get(k, 0):>10}{flag}")

    pv, fv = sum(persist.values()), sum(fresh.values())
    pz = sum(1 for v in persist.values() if v == 0)
    fz = sum(1 for v in fresh.values() if v == 0)
    print(f"\nrooms recovered   persistent {pv:>4}   per-hotel {fv:>4}")
    print(f"properties at 0   persistent {pz:>4}/{len(persist)}   "
          f"per-hotel {fz:>4}/{len(fresh)}")
    print(f"wall clock        persistent {t_p1+t_p2:>6.0f}s   "
          f"per-hotel {t_h1+t_h2:>6.0f}s")
    if pv == fv:
        print("\nVERDICT: no measurable difference in what was recovered.")
    else:
        better = "persistent" if pv > fv else "per-hotel"
        print(f"\nVERDICT: {better} recovered more ({abs(pv-fv)} rooms).")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 8)
