"""Pipeline driver. One city in, correct room imagery in Bookme's DB out.

    python -m pipeline.run                              interactive wizard
    python -m pipeline.run --city Dubai --limit 20
    python -m pipeline.run --city Dubai --city-id 1280 --yes --rooms-from both
    python -m pipeline.run --city Dubai --limit 5 --dry-run

Omit --city and the wizard prompts for a city name or id, shows every matching
city_id with how many of its hotels are runnable right now vs already published
(config.LEDGER_STALE_DAYS), and asks whether to bound the run or take the whole
city -- 'b' steps back a question at any point. Automation (cron, etc.) should
pass --city and skip the wizard entirely; --city-id/--yes/--rooms-from/--limit
only mean anything alongside --city.

Hotel identity comes from `v2_common_hotels` -- name, slug, address and
coordinates are all in the database, so finding out WHO exists costs nothing.
The Bookme public API is used for exactly one thing the database cannot supply:
live room NAMES. Agoda supplies the room IMAGES, matched name-by-name and
geo-verified so a picture never lands on the wrong building.

The unit of atomicity is ONE HOTEL: its rooms and their attachments commit
together, and only then is it written to the ledger. Interrupt with Ctrl-C and
the hotel in flight finishes and commits before the process exits; re-running
resumes at the first hotel the ledger does not know about.
"""
import argparse
import asyncio
import collections
import csv
import datetime
import functools
import hashlib
import json
import os
import random
import re
import shutil
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from . import (agoda, agoda_browser, booking, bookme, categories, config, cos,
               db, ledger, match)

# Redirected to a file (nohup, cron -- every real deployment), stdout is FULLY
# block-buffered by default, so agoda.py's "cooling down 420s" warnings sit
# invisible in the buffer and a rate-limit stall looks identical to a hang.
sys.stdout.reconfigure(line_buffering=True)

OUT = os.path.join(config.ROOT, "out")
CACHE = os.path.join(OUT, "cache")

# Circuit breaker for "Agoda stopped answering". Counted in HOTELS, not
# requests, because that is the unit an operator watches and the unit the
# damage is measured in. Rotate first (a fresh session sometimes clears a
# soft block), abort only once it is clearly not transient -- by which point
# agoda.py's own escalating cooldown has already spent 420+840+1680s standing
# down, so reaching the abort means roughly an hour of genuine unavailability.
AGODA_SICK_ROTATE = 3
AGODA_SICK_ABORT = 8

# Coordinates are the strongest evidence when both sides have good ones -- but
# a wholesale feed's coordinates are sometimes kilometres off (Raha Grand Hotel:
# identical name, 9.6km apart, same building). So distance CONFIRMS, and city
# agreement plus a strict name score is the fallback that rescues bad-geo
# records without letting in the Narita/Danang namesakes.
NEAR_KM = 0.35
FAR_KM = 1.50
NAME_OK = 72          # recall metric: floor to bother fetching a candidate
NAME_STRONG = 90      # recall metric, used alongside a plausible distance
NAME_STRICT = 88      # precision metric, required when relying on city alone

# How close a Bookme search result must sit to the DB row to be called the same
# hotel when the slug ladder has already failed. Tighter than FAR_KM because
# here we are identifying a hotel against our OWN authoritative coordinates,
# not reconciling two third parties.
SAME_HOTEL_KM = 0.35

_STOP = False

LOCK_PATH = os.path.join(OUT, ".run.lock")


def acquire_run_lock():
    """Refuse to start if another run of this pipeline is already going.

    This is not politeness, it is the one damage scenario the design cannot
    undo. `v2_rooms` has NO unique constraint (verified: only PRIMARY on `id`
    plus two non-unique indexes), so the dedupe that stops a re-run
    duplicating rooms is `existing_room_names()` READ, then INSERT -- a
    check-then-act that is only atomic against itself inside one transaction,
    not against a second process doing the same thing concurrently. Two runs
    covering the same hotel would both read "no such room", both insert, and
    the site would show every room twice.

    And that damage is PERMANENT here: cleaning it up needs a DELETE, which
    this pipeline is categorically forbidden from issuing. So the cheap
    upstream guard is the only real defence.

    flock is advisory and released automatically when the process exits --
    including on a crash or a kill -9 -- so a dead run never leaves a stale
    lock that needs manual clearing.
    """
    os.makedirs(OUT, exist_ok=True)
    fh = open(LOCK_PATH, "w")
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise SystemExit(
            f"another pipeline run already holds {LOCK_PATH}.\n"
            f"Two concurrent runs can double-insert rooms (v2_rooms has no "
            f"unique constraint) and that CANNOT be cleaned up, because this "
            f"pipeline may not DELETE. Wait for the other run to finish."
        ) from None
    fh.write(f"{os.getpid()}\n")
    fh.flush()
    return fh                      # kept open for the process's lifetime


def _on_sigint(*_):
    """Graceful stop: never abandon a hotel mid-transaction."""
    global _STOP
    if _STOP:                       # second Ctrl-C means "now"
        raise KeyboardInterrupt
    _STOP = True
    print("\n  stop requested -- finishing the hotel in flight, then exiting "
          "(Ctrl-C again to abort immediately)")


def _on_sigterm(*_):
    """Same graceful stop, for the signal a MACHINE sends.

    SIGTERM -- not SIGINT -- is what `systemctl stop`, `docker stop`, a
    Kubernetes eviction and a cloud instance shutdown all send, and it is the
    realistic way this process dies in production; Ctrl-C is the developer's
    case. Unhandled, SIGTERM kills the process instantly: a hotel mid-publish
    loses nothing (the transaction rolls back and the hotel is simply re-run)
    but a hotel that just committed and had not yet reached the ledger IS
    re-run needlessly, and every room name harvested this pass is thrown away.
    Handled, the shutdown grace period is spent finishing the hotel in flight
    and check-pointing, which is what that grace period is for.
    """
    global _STOP
    if _STOP:
        raise KeyboardInterrupt
    _STOP = True
    print("\n  SIGTERM received (shutdown) -- finishing the hotel in flight "
          "and check-pointing, then exiting")


# ------------------------------------------------------------------- dates
def weekend_checkin(weeks_out=None, today=None):
    """The Saturday `weeks_out` weekends past the coming one.

    See config.STAY_WEEKS_OUT for why a weekend, and why not the coming one.
    Always strictly in the future: on a Saturday, "the coming Saturday" is seven
    days away, so a re-run never probes a date that has already started.
    """
    weeks_out = config.STAY_WEEKS_OUT if weeks_out is None else weeks_out
    d = today or datetime.date.today()
    days_to_saturday = (5 - d.weekday()) % 7 or 7
    return d + datetime.timedelta(days=days_to_saturday + 7 * weeks_out)


def stay(weeks_out=None, nights=config.STAY_NIGHTS):
    d = weekend_checkin(weeks_out)
    return d.isoformat(), (d + datetime.timedelta(days=nights)).isoformat()


# ------------------------------------------------------- agoda property cache
def _probe_tag(check_in=None, adults=None):
    return f"{check_in or stay()[0]}/{config.ADULTS if adults is None else adults}ad"


def _cache_agoda(prop, probe=None):
    """Cache one Agoda property. Matching already downloads the full room grid
    to geo-verify a candidate, so keeping it saves the room stage a ~800KB
    re-fetch -- and makes a crashed run cheap to resume.

    Two kinds of fact age differently here. Coordinates, city, slug and isNHA
    are date-INVARIANT and true until the building changes. `rooms` and
    `supplier_count` were fetched for one night and occupancy because the grid
    is availability-scoped. Rooms are NOT discarded merely because a later run
    probes a different date -- a room seen on any night is a real room, the same
    identity-not-availability reasoning that makes the multi-probe union sound.
    What they cannot be is indefinitely old, so each list is stamped and
    `_cached_agoda` expires it on AGE.
    """
    os.makedirs(CACHE, exist_ok=True)
    rec = dict(prop)
    if rec.get("rooms"):
        rec["rooms_probe"] = probe or _probe_tag()
        rec["rooms_fetched_at"] = datetime.date.today().isoformat()
    with open(os.path.join(CACHE, f"agoda_{prop['agoda_id']}.json"), "w",
              encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False)


def _cached_agoda(agoda_id, fresh_days=None):
    """Cached property. Pass `fresh_days` when the ROOMS will be used: a room
    list older than that is dropped so the caller re-fetches, while the
    date-invariant metadata (what matching needs) is always returned."""
    assert fresh_days is None or isinstance(fresh_days, int), (
        f"fresh_days must be a day count, got {type(fresh_days).__name__}")
    p = os.path.join(CACHE, f"agoda_{agoda_id}.json")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        rec = json.load(f)
    if fresh_days is None or not rec.get("rooms"):
        return rec
    taken = rec.get("rooms_fetched_at")
    if not taken:                     # cached before provenance was recorded
        return {**rec, "rooms": [], "rooms_expired": "unknown age"}
    age = (datetime.date.today() - datetime.date.fromisoformat(taken)).days
    if age > fresh_days:
        return {**rec, "rooms": [], "rooms_expired": f"{age}d old"}
    return rec


# --------------------------------------------- stage 1: bookme room harvest
PROBE_CACHE = os.path.join(CACHE, "bookme_rooms.json")


def _probe_scope(hotels):
    """A fingerprint of exactly WHICH hotels a checkpoint covers.

    Load-bearing, not decoration. `done_shapes` records "this shape is
    finished"; that claim is only true for the hotel set it was measured over.
    Without this key, a Dubai run interrupted half-way would leave a checkpoint
    that a subsequent Vienna run loads -- and Vienna would skip shapes it never
    ran, reporting hotels as probed-and-empty that were never asked at all.
    That is precisely the unearned zero the rest of this pipeline refuses to
    make, arrived at through the cache instead of the network.

    The probe LADDER is part of the identity too: re-tuning config.ROOM_PROBES
    must invalidate a checkpoint, or the new shapes are skipped as "done".
    """
    ids = sorted(h["id"] for h in hotels)
    payload = json.dumps([ids, config.ROOM_PROBES,
                          config.ROOM_PROBES_ESCALATION,
                          config.PROBE_MIDWEEK_OFFSET_DAYS],
                         sort_keys=True).encode()
    return hashlib.md5(payload).hexdigest()


def _save_probe_state(scope, live_rooms, resolved, unavailable, done_shapes):
    """Check-point the harvest so a restart resumes instead of re-probing.

    The whole pass is in-memory otherwise, so a machine that reboots 20 minutes
    into a 27-minute city harvest starts from nothing. This is the same
    reasoning that made _cache_agoda worth having, applied to the other
    platform -- and the same reasoning that makes it SAFE: a room seen on any
    night is a real room, so a partial harvest is never wrong, only incomplete.

    Written atomically (temp file + os.replace) because the thing being
    protected against is a crash: a half-written checkpoint that the next run
    then failed to parse would be strictly worse than no checkpoint at all.
    os.replace is atomic on POSIX, so a reader sees either the whole old file
    or the whole new one, never a torn one.
    """
    os.makedirs(CACHE, exist_ok=True)
    tmp = f"{PROBE_CACHE}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"scope": scope,
                   "rooms": {str(k): list(v.values())
                             for k, v in live_rooms.items()},
                   "resolved": sorted(resolved),
                   "unavailable": sorted(unavailable),
                   "done_shapes": sorted(done_shapes),
                   "saved_at": datetime.datetime.now().isoformat(timespec="seconds")},
                  f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, PROBE_CACHE)


def _load_probe_state(scope, live_rooms):
    """Restore a check-pointed harvest into `live_rooms`. Returns
    (resolved, unavailable, done_shapes).

    A checkpoint for a DIFFERENT hotel set or a different probe ladder is
    ignored entirely -- see _probe_scope. A cache that will not parse is
    discarded rather than raised: re-probing is slow, crashing is worse.
    """
    if not os.path.exists(PROBE_CACHE):
        return set(), set(), set()
    try:
        with open(PROBE_CACHE, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("scope") != scope:
            print("  probe checkpoint is for a different hotel set or probe "
                  "ladder; ignoring it and harvesting fresh")
            return set(), set(), set()
        for hid, rooms in (d.get("rooms") or {}).items():
            acc = live_rooms.setdefault(int(hid), {})
            for r in rooms:
                acc.setdefault(r["room_name"], r)
        return (set(d.get("resolved") or []), set(d.get("unavailable") or []),
                set(d.get("done_shapes") or []))
    except Exception as e:
        print(f"  probe checkpoint unreadable ({type(e).__name__}); "
              f"starting the harvest fresh")
        return set(), set(), set()


# --------------------------------------- stage 3a: discovery/commit split
# Matching (Agoda + Booking) is cheap network calls; committing is downloading
# and re-hosting real image bytes. Interleaving them per-hotel is why a hotel
# with a big gallery makes the NEXT hotel's matching look stalled -- the
# operator is actually watching image transfer, not discovery.
#
# So discovery runs for every hotel FIRST, writing what it finds to a plan
# (this checkpoint), and only after that does a second pass mirror images and
# publish. Same shape, same reasoning, as the Bookme probe checkpoint above:
# resumable per unit of work, invalidated when the hotel set or the tuning
# that produced it changes, discarded on a clean finish.
#
# Row-per-ROOM CSV, hotel columns repeated -- the same flat shape already used
# for rooms_review.csv/rooms_unmatched.csv, not a new convention.
PLAN_CACHE = os.path.join(CACHE, "plan.csv")
PLAN_COLUMNS = ["city_id", "city_name", "hotel_id", "slug", "hotel_name",
               "bm_label", "ag_count", "ag_source", "n_review", "n_unmatched",
               "room_name", "category", "category_id", "size_sqft",
               "source_images", "image_source"]


def _plan_scope(hotels):
    """Same load-bearing role as _probe_scope: a plan checkpoint is only valid
    for the hotel set AND the matching/gap-fill tuning that produced it. A
    config change (a tightened Booking gate, a new review threshold) must
    invalidate a stale plan rather than silently reuse rooms matched under the
    old rules."""
    ids = sorted(h["id"] for h in hotels)
    payload = json.dumps([ids, config.ROOM_ACCEPT, config.ROOM_REVIEW,
                          config.BOOKING_MIN_NAME, config.BOOKING_MAX_KM,
                          config.BOOKING_ENABLED],
                         sort_keys=True).encode()
    return hashlib.md5(payload).hexdigest()


def _load_plan(scope):
    """(rows_by_hotel_id, planned_hotel_ids) from a checkpoint, or ({}, set())
    if there is none or it does not match this run's scope. Each value in
    rows_by_hotel_id is that hotel's list of raw CSV row dicts, in the order
    written -- reconstruction into room dicts happens at the CALL SITE
    (Phase 1 preview vs Phase 2 commit want slightly different shapes from the
    same rows), not here."""
    if not os.path.exists(PLAN_CACHE):
        return {}, set()
    # csv.reader + manual width check, NOT csv.DictReader -- a torn last row
    # (a crash mid-write) has FEWER fields than the header, and DictReader
    # fills the missing ones with None rather than rejecting the row. That
    # silently hands a room dict full of Nones into reconstruction instead of
    # dropping the one row a crash actually damaged -- the same class of bug
    # ledger.py's own loader was built to close (see its docstring), applied
    # here rather than re-learned.
    try:
        with open(PLAN_CACHE, newline="", encoding="utf-8", errors="replace") as f:
            raw = list(csv.reader(f))
    except (OSError, csv.Error) as e:
        print(f"  plan checkpoint unreadable ({type(e).__name__}); "
              f"re-discovering fresh")
        return {}, set()
    if not raw:
        return {}, set()
    header, data = raw[0], raw[1:]
    if header != PLAN_COLUMNS:
        print("  plan checkpoint has a different schema; ignoring it "
              "and re-discovering fresh")
        return {}, set()
    rows, skipped = [], 0
    for fields in data:
        if len(fields) != len(PLAN_COLUMNS):
            skipped += 1
            continue
        rows.append(dict(zip(PLAN_COLUMNS, fields)))
    if skipped:
        print(f"  plan checkpoint: skipped {skipped} malformed row(s) (likely "
              f"a torn write from an interrupted run); the rest loaded normally")
    # The scope is not stored IN the CSV (a hotel-by-hotel row shape has
    # nowhere clean to put a single run-wide value without repeating it on
    # every row); it lives in a sidecar instead, exactly like PROBE_CACHE's
    # `scope` field but out-of-band since this file's shape is tabular.
    scope_path = PLAN_CACHE + ".scope"
    try:
        with open(scope_path, encoding="utf-8") as f:
            saved_scope = f.read().strip()
    except OSError:
        saved_scope = None
    if saved_scope != scope:
        print("  plan checkpoint is for a different hotel set or matching "
              "config; ignoring it and re-discovering fresh")
        return {}, set()
    by_hotel = {}
    for r in rows:
        try:
            hid = int(r["hotel_id"])
        except ValueError:
            continue          # right width, garbage id -- corrupt, not fatal
        by_hotel.setdefault(hid, []).append(r)
    return by_hotel, set(by_hotel)


def _save_plan_scope(scope):
    os.makedirs(CACHE, exist_ok=True)
    with open(PLAN_CACHE + ".scope", "w", encoding="utf-8") as f:
        f.write(scope)


def _append_plan_rows(hotel, to_publish, bm_label, ag_count, ag_source,
                      n_review, n_unmatched):
    """Check-point ONE hotel's discovery result. Called immediately after that
    hotel's mapping finishes, never batched -- discovery's per-hotel cost is
    already dominated by network round-trips, so a per-hotel fsync is cheap
    against that, and it is the tightest resumability granularity available:
    a crash loses at most the hotel in flight, never a whole batch.

    Plain append, not the temp-file+os.replace dance _save_probe_state uses --
    that protects a REWRITE of the whole file; this only ever grows it, so the
    worst a crash mid-write can do is a torn LAST row, which _load_plan's
    csv.DictReader simply drops (short row, KeyError on read -- caught in the
    per-hotel reconstruction, not here) -- the same tolerance ledger.py already
    relies on for its own append-only files.
    """
    os.makedirs(CACHE, exist_ok=True)
    new = not os.path.exists(PLAN_CACHE)
    with open(PLAN_CACHE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=PLAN_COLUMNS)
        if new:
            w.writeheader()
        for r in to_publish:
            w.writerow({
                "city_id": hotel["city_id"], "city_name": hotel.get("city_name", ""),
                "hotel_id": hotel["id"], "slug": hotel.get("slug", ""),
                "hotel_name": hotel.get("name", ""),
                "bm_label": bm_label, "ag_count": ag_count, "ag_source": ag_source,
                "n_review": n_review, "n_unmatched": n_unmatched,
                "room_name": r["name"], "category": r["category"],
                "category_id": r["category_id"], "size_sqft": r["size_sqft"],
                "source_images": "|".join(r["source_images"]),
                "image_source": r.get("image_source", "")})
        f.flush()
        os.fsync(f.fileno())


def _room_from_plan_row(r, cat_ids):
    """One plan.csv row -> a _room()-shaped dict. Mirrors
    _room_from_review_row exactly -- same reconstruction, different source
    file, so a Phase 2 commit produces category_id the identical way a live
    run always has (re-classified from cat_ids, never trusted verbatim off
    disk, in case cat_ids itself changed between the plan and the commit)."""
    images = [u for u in (r.get("source_images") or "").split("|") if u]
    size = r.get("size_sqft") or None
    room = _room(r["room_name"], images, cat_ids, size_sqft=int(size) if size else None)
    room["image_source"] = r.get("image_source", "")
    return room


def _phase2_skip_ids(led):
    """Hotel ids Phase 2 should NOT re-walk: published AND with no open
    issue. Same rule the top of main() already applies when selecting `todo`
    in the first place -- a hotel that published but still owes some rooms a
    picture (`needs_image_backfill`) must stay in play, not be skipped as if
    it were finished. Read fresh at call time, not a value captured earlier:
    an arbitrary amount of time, or an earlier Phase 2 attempt within the
    SAME invocation's deferred-retry pass, may have published hotels since.
    """
    return led.fresh_ids() - led.unresolved_ids()


def probe_shapes(shapes):
    """Config's (weeks_out, adults, nights) tuples -> concrete probe params.

    Each shape yields a weekend AND its midweek counterpart: business hotels
    release different inventory midweek, and a hotel invisible on a Saturday can
    be fully open on a Tuesday. Returns (check_in, check_out, adults, label).
    """
    out = []
    for weeks, adults, nights in shapes:
        base = weekend_checkin(weeks)
        for tag, ci in (("wknd", base),
                        ("midwk", base + datetime.timedelta(
                            days=config.PROBE_MIDWEEK_OFFSET_DAYS))):
            co = ci + datetime.timedelta(days=nights)
            out.append((ci, co, adults,
                        f"+{weeks}w {tag} {adults}ad {nights}n"))
    return out


def _probe_one(bs, hotel, ci, co, adults):
    """One (hotel, shape) call. Returns (rooms, state).

    state is one of:
      "ok"          -- Bookme answered; `rooms` is what it offered (may be empty)
      "unavailable" -- Bookme's permanent 500. A fact about the property.
      "error"       -- we failed to ASK. Never conflated with an empty answer,
                       because an unearned zero is indistinguishable downstream
                       from a hotel that genuinely has no rooms.
    """
    try:
        return bookme.availability(bs, hotel["slug"], ci, co, adults=adults), "ok"
    except bookme.Unavailable:
        return [], "unavailable"
    except bookme.AuthFailed:
        raise                       # fatal for the bookme side; do not swallow
    except Exception as e:
        print(f"    {(hotel.get('name') or '')[:34]:36} probe failed "
              f"({type(e).__name__}: {str(e)[:60]})")
        return [], "error"


def harvest_rooms(bs, hotels, live_rooms, probe_log, label_prefix=""):
    """Live Bookme room names for a set of hotels, unioned over every shape.

    Keyed by SLUG -- no city search, no polling, no ref ids. `live_rooms` is a
    {hotel_id: {room_name: record}} accumulator, mutated in place so a run that
    dies mid-pass keeps everything already earned.

    Two properties this function must hold, both learned the hard way:

    * UNION, never best-one-wins. The endpoint is heavily nondeterministic --
      six identical calls to one slug returned 19/18/27/38/36/44 rooms. Over 60
      hotels x 4 shapes the union found +46% more rooms than the best single
      shape, and had not plateaued. One call is not a measurement of a hotel.

    * A ZERO IS NEVER ACCEPTED AS-IS. Every hotel runs the full base ladder, and
      any hotel still holding nothing then runs the escalation ladder, which
      widens along different axes rather than repeating the same ones. Only a
      hotel that answered "nothing" to every shape askable is recorded as having
      no rooms. This is the same rule `_escalate` enforces on the Agoda side.

    Every completed shape is CHECK-POINTED to disk, so a machine that reboots
    part-way through resumes at the next shape instead of re-probing from zero.

    Returns (resolved_ids, unavailable_ids) -- hotels Bookme answered for at all,
    and hotels it affirmatively reported as not sellable.
    """
    errored = set()
    todo = [h for h in hotels if (h.get("slug") or "").strip()]
    scope = _probe_scope(todo)
    resolved, unavailable, done_shapes = _load_probe_state(scope, live_rooms)
    if done_shapes:
        print(f"  {label_prefix}resuming from checkpoint: "
              f"{len(done_shapes)} shape(s) already done, "
              f"{sum(len(v) for v in live_rooms.values())} room names held")
    if not todo:
        return resolved, unavailable

    def run_pass(batch, shapes, phase):
        for ci, co, adults, shape in shapes:
            if _STOP:
                print(f"  {label_prefix}stopping probes as requested")
                return
            tag = f"{phase}|{shape}|{ci.isoformat()}"
            if tag in done_shapes:            # already survived a previous run
                continue
            # Hotels Bookme has already called permanently dead are dropped from
            # later shapes -- verified permanent across 25 consecutive calls and
            # 3 further dates, so re-asking buys nothing but quota.
            live = [h for h in batch
                    if not (config.TRUST_PERMANENT_UNAVAILABLE
                            and h["id"] in unavailable)]
            if not live:
                return
            t0 = time.time()
            gained = states = 0
            counts = {"ok": 0, "unavailable": 0, "error": 0}
            with ThreadPoolExecutor(max_workers=config.ROOM_PROBE_WORKERS) as ex:
                results = list(ex.map(
                    lambda h: _probe_one(bs, h, ci, co, adults), live))
            for h, (rooms, state) in zip(live, results):
                counts[state] += 1
                states += 1
                if state == "unavailable":
                    unavailable.add(h["id"])
                    continue
                if state == "error":
                    errored.add(h["id"])
                    continue
                resolved.add(h["id"])
                errored.discard(h["id"])
                acc = live_rooms.setdefault(h["id"], {})
                for r in rooms:
                    # EVERY raw name is kept, rate-plan variants included. They
                    # are all real rows on Bookme's side and all need the
                    # corrected imagery; consolidating them here would publish
                    # one and silently strand the rest on their old wrong photo.
                    # Variants are grouped for MATCHING only -- see map_rooms().
                    if r["room_name"] not in acc:
                        acc[r["room_name"]] = r
                        gained += 1
            el = time.time() - t0
            probe_log.append({
                "phase": phase, "shape": shape, "check_in": ci.isoformat(),
                "check_out": co.isoformat(), "adults": adults,
                "hotels_probed": states, "new_rooms": gained,
                "answered": counts["ok"], "unavailable": counts["unavailable"],
                "errors": counts["error"], "seconds": round(el, 1)})
            print(f"  {label_prefix}{phase} {shape:22} {states:4} hotels -> "
                  f"+{gained:4} new room names  "
                  f"({counts['ok']} answered, {counts['unavailable']} unavailable"
                  + (f", {counts['error']} errors" if counts["error"] else "")
                  + f", {el:.0f}s)")
            # Check-point AFTER the shape completes, never mid-shape: a shape is
            # the unit that can be cheaply redone, and marking one done before
            # its results are in the accumulator would lose exactly the rooms it
            # found. Failure to write the checkpoint costs speed on a later
            # resume, never correctness, so it must not take the run down.
            done_shapes.add(tag)
            try:
                _save_probe_state(scope, live_rooms, resolved, unavailable,
                                  done_shapes)
            except OSError as e:
                print(f"  {label_prefix}could not write probe checkpoint "
                      f"({type(e).__name__}); continuing without resume support")

    run_pass(todo, probe_shapes(config.ROOM_PROBES), "base")

    # A zero is only believable once every shape has been asked. Anything still
    # empty -- including hotels that only ever errored -- earns the wider ladder.
    empty = [] if _STOP else [
        h for h in todo if not live_rooms.get(h["id"])
        and not (config.TRUST_PERMANENT_UNAVAILABLE and h["id"] in unavailable)]
    if empty:
        print(f"  {label_prefix}{len(empty)} hotel(s) still hold zero rooms; "
              f"escalating with {len(config.ROOM_PROBES_ESCALATION)} wider shapes")
        run_pass(empty, probe_shapes(config.ROOM_PROBES_ESCALATION), "escalation")

    still_error = {h["id"] for h in todo
                   if h["id"] in errored and not live_rooms.get(h["id"])}
    if still_error:
        print(f"  {label_prefix}WARNING: {len(still_error)} hotel(s) could not be "
              f"ASKED on any shape (network/API failures, not empty answers). "
              f"They are reported as errors, never as 'no rooms'.")
    return resolved, unavailable


# ------------------------------------------------------- stage 2: agoda match
# Connector words that carry no signal in a country name. Without excluding
# them, "Bosnia and Herzegovina" yields initials "BaH" and "Saint Kitts and
# Nevis" yields "SKaN" -- junk patterns matching nothing real, which can only
# ever delete a legitimate token that happens to start with those letters.
_NAME_CONNECTORS = {"and", "the", "of", "des", "del", "dos", "das", "der",
                    "den", "und", "les", "los", "las", "van", "bin", "sur"}


def _strip_country_token(s, pattern):
    """Remove the ONE occurrence of `pattern` that is the country, not a city.

    A country name is not a safe global find-and-replace, because plenty of
    cities embed their country's name -- Panama City, Mexico City, Kuwait
    City, Guatemala City -- and blanket removal turns "Panama City, Panama"
    into a bare "City", deleting the single most useful token in the string.
    City-states (Singapore, Luxembourg, Monaco, Djibouti) are the same problem
    in the extreme: the country name IS the city name.

    So: consider only occurrences that are not immediately followed by "city",
    and drop the LAST of those. Measured on 4,000 real addresses, 90% end with
    their country, so the last non-city occurrence is the country in the
    overwhelming majority -- and where it is not (the 10% that put the city
    afterwards) removing that one occurrence still leaves the city intact,
    because removal never touches the rest of the string.
    """
    spans = [m.span() for m in re.finditer(r"\b" + pattern + r"\b", s,
                                           flags=re.IGNORECASE)
             if not re.match(r"\s*city\b", s[m.end():], flags=re.IGNORECASE)]
    if not spans:
        return s
    a, b = spans[-1]
    return s[:a] + " " + s[b:]


def _address_query(address, country_name=None, city_name=None):
    """Locality tokens from a postal address, for a place-name index.

    REMOVES the noise rather than TRUNCATING at it, and that distinction is the
    entire design. Measured over 4,000 real addresses from this catalogue: 93%
    carry a 4+ digit postcode and 90% end with their country name -- but field
    ORDER is not universal. "vardanants 15/4, 0010, armenia, yerevan" puts the
    city LAST, after both the postcode and the country.

    Truncating at the first postcode therefore deletes the city on exactly
    those addresses, and left 5% of the catalogue (~4,400 hotels) with a string
    too short to query at all -- the last-resort match path silently dead for
    entire address styles, which is how the previous version behaved for
    Bangladesh and Panama.

    Removal cannot do that. It is order-independent, so no field position can
    make it lose the city, and it degrades gracefully when a token it hoped to
    strip is spelled differently than expected -- real rows carry "bosnia and
    herzegowina" against a database spelling of "Herzegovina", where the junk
    token merely survives alongside every locality token instead of taking the
    whole string down with it.
    """
    if not address:
        return None
    s = address
    # When the city IS the country, the country name is the locality, not
    # noise -- stripping it deletes the only place token in the string. Not an
    # edge case here: 8,613 hotels (~10% of the catalogue) sit in cities whose
    # name equals their country, because this database stores several whole
    # countries as single cities (Brazil alone holds 6,732).
    if (country_name and city_name
            and match.norm(country_name) == match.norm(city_name)):
        country_name = None
    if country_name:
        words = [w for w in re.split(r"\s+", country_name.strip())
                 if len(w) > 2 and w.lower() not in _NAME_CONNECTORS]
        s = _strip_country_token(s, re.escape(country_name))
        if len(words) > 1:                      # UAE, USA, UK ...
            initials = "".join(w[0] for w in words)
            s = _strip_country_token(s, r"\.?".join(initials) + r"\.?")
    s = re.sub(r"[^\w\s-]", " ", s)             # punctuation -> space
    # Standalone 4+ digit runs are postcodes or large building numbers, both
    # noise to a PLACE index. Attached forms ("15/4", "3380stawell") are left
    # alone, because splitting those destroys real tokens.
    s = re.sub(r"\b\d{4,}\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:80] if len(s) > 8 else None


def match_hotel(s, h, check_in, check_out, city=None, destination=None,
                country_name=None, top_n=5):
    """Resolve one DB hotel to an Agoda property.

    Chain brands defeat name-only matching: "Golden Sands Hotel Apartments"
    scores 100% against Golden Sands 3, 5 AND 10 -- three different buildings.
    So every viable candidate is fetched and the geographically closest wins.
    This must run to the end of top_n rather than stopping at the first
    candidate inside some radius: an individually-listed vacation-rental UNIT
    can sit in the same building as the hotel it is named after ("Luxury Burj
    View...Kempinski Central Avenue...", 40m from the real Kempinski), scoring
    100% on name and landing well inside any plausible cutoff. Those are
    filtered by Agoda's own isNHA flag, because neither name nor distance can.
    """
    dest = destination or config.DESTINATION
    plain = re.sub(r"\s*\([^)]*\)", "", h["name"] or "").strip()
    # Rebrand parentheticals derail the suggest index and the destination
    # disambiguates global namesakes -- but both are ESCALATIONS, not defaults:
    # issuing all three queries for every hotel tripled the request rate and
    # tripped Agoda's rate limiter a third of the way through a 465-hotel run.
    queries = [plain]
    if plain != (h["name"] or ""):
        queries.append(h["name"])
    if dest and dest.lower() not in plain.lower():
        queries.append(f"{plain} {dest}")
    # Last resort: the ADDRESS. Agoda's suggest index covers address text, so a
    # hotel whose name it will not surface can still be reached through where it
    # physically is. This only ADDS candidates -- each still faces the isNHA,
    # distance and name-score gates below.
    if h.get("address"):
        addr = _address_query(h["address"], country_name, dest)
        if addr:
            queries.append(addr)

    bn = match.norm(plain)
    cands, errs = {}, []
    for q in queries:
        try:
            for c in agoda.suggest(s, q):
                cands.setdefault(c["agoda_id"], c)
        except Exception as e:
            errs.append(str(e))
        if any(match.score(bn, match.norm(c["agoda_name"])) >= NAME_STRONG
               for c in cands.values()):
            break
    if not cands:
        # `unreachable` separates "we never got to ask" from "we asked and this
        # hotel is not on Agoda". They are the same empty candidate list and
        # completely different facts: one is an outage, the other is a property.
        # Collapsing them is what let a two-hour Agoda block be written into 12
        # hotels' reports as "no agoda match".
        if errs:
            return {"reason": f"suggest failed: {errs[0]}", "unreachable": True}
        return {"reason": "no agoda suggestion"}

    ranked = sorted(cands.values(),
                    key=lambda c: -match.score(bn, match.norm(c["agoda_name"])))
    best, best_reject = None, None
    for c in ranked[:top_n]:
        ns = match.score(bn, match.norm(c["agoda_name"]))
        if ns < NAME_OK:
            break                                   # ranked desc, rest are worse
        # Everything decided here -- coordinates, city, isNHA -- is date-invariant
        # property metadata, so a candidate verified by an earlier run answers
        # again for free. The payload is 803KB to reach a 27-byte verdict, and
        # re-runs are the norm because rate limiting interrupts long matches.
        p = _cached_agoda(c["agoda_id"])
        if p is None:
            try:
                p = agoda.property_rooms(s, c["agoda_id"], check_in, check_out)
            except Exception as e:
                best_reject = best_reject or f"agoda fetch failed: {e}"
                continue
            if p:
                # Cache on fetch, not on acceptance: a rejected candidate cost
                # exactly as much, and the reason it was rejected will not change.
                _cache_agoda(p)
        if not p:
            best_reject = best_reject or "agoda property returned no data"
            continue
        if p.get("is_nha"):
            best_reject = best_reject or (
                f"rejected {c['agoda_name']!r}: individually-listed accommodation "
                f"({p.get('accommodation_type')}), not the hotel")
            continue

        d = agoda.km(h.get("lat"), h.get("lon"), p["lat"], p["lon"])
        strict = match.strict_score(bn, match.norm(c["agoda_name"]))
        same_city = city is not None and p.get("city_id") == city
        conf = _confidence(ns, d, strict, same_city)
        if not conf:
            best_reject = best_reject or (
                f"rejected {c['agoda_name']!r} name={ns:.0f}% strict={strict:.0f}% "
                f"dist={'?' if d is None else f'{d:.2f}km'} same_city={same_city}")
            continue
        key = (float("inf") if d is None else d, -ns)
        if best is None or key < best[0]:
            best = (key, c, p, ns, d, conf)

    if best is None:
        return {"reason": best_reject or "no candidate above name threshold"}
    _, c, p, ns, d, conf = best
    return {"agoda_id": p["agoda_id"],
            "agoda_name": p["agoda_name"] or c["agoda_name"],
            "agoda_url": f"https://www.agoda.com/search?hotel={p['agoda_id']}",
            "slug": p.get("slug"), "name_score": round(ns, 1),
            "distance_km": round(d, 3) if d is not None else None,
            "confidence": conf}


def _confidence(name_score, dist_km, strict=0, same_city=False):
    """high  = coordinates agree, so the building is proven.
       medium = same city and a strict name match, or a plausible distance.
       None   = reject."""
    if dist_km is not None and dist_km <= NEAR_KM and name_score >= NAME_OK:
        return "high"
    if same_city and strict >= NAME_STRICT:
        return "medium"
    if dist_km is not None and dist_km <= FAR_KM and name_score >= NAME_STRONG:
        return "medium"
    if dist_km is None and name_score >= NAME_STRONG:
        return "medium"
    return None


# ------------------------------------------------------- stage 3: agoda rooms
def agoda_rooms(ags, m, country_iso, destination):
    """Every Agoda room for a matched property, over HTTP, browser, then a
    widening ladder -- unioned, never first-success.

    Both platforms resell the same physical hotel, so a thin grid is a statement
    about the parameters asked under, not about the hotel.
    """
    hid = m["agoda_id"]
    cached = _cached_agoda(hid, fresh_days=config.CACHE_FRESH_DAYS) or {}
    rooms = cached.get("rooms") or []
    # Trust the cache only when it was filled by a COMPLETE ladder run, never
    # because it happens to hold "enough" rooms. The old test was
    # `len(rooms) >= ROOM_RETRY_BELOW`, which declared a hotel finished at an
    # arbitrary count while later rungs were still finding more -- and, worse,
    # made the answer depend on where a partial earlier run happened to stop.
    # `ladder_complete` records what actually happened instead of inferring it.
    if rooms and cached.get("ladder_complete"):
        return rooms, "cache"

    source = "http"
    # No country code means no URL that can land. Attempting it anyway would
    # return an empty grid indistinguishable from a genuinely empty hotel --
    # so it is skipped deliberately rather than run on a guess.
    if not rooms and country_iso:
        # The HTTP grid can come back empty for a property the website renders
        # perfectly well; the browser fallback reads the page's own XHR off the
        # wire rather than scraping the DOM.
        # ponytail: one chromium launch per blind hotel (~4s) instead of one
        # session for all of them, which is the price of publishing hotel by
        # hotel so a crash cannot lose a day's work. If blind hotels turn out
        # to be common, collect them and run a second batched pass at the end.
        check_in, check_out = stay()
        try:
            rec = asyncio.run(agoda_browser.fetch_rooms(
                [{"agoda_id": hid, "agoda_name": m["agoda_name"],
                  "slug": m.get("slug") or cached.get("slug"),
                  "city": destination}],
                check_in, check_out, country_code=country_iso))
        except Exception as e:
            print(f"    browser fallback failed ({type(e).__name__}: {e})")
            rec = {}
        got = rec.get(hid) or rec.get(str(hid)) or []
        if got:
            rooms, source = got, "browser"
            _cache_agoda({**cached, "agoda_id": hid, "rooms": rooms,
                          "source": "browser"}, probe=_probe_tag())

    # The ladder ALWAYS runs, and the measurements below are the reason -- they
    # also refute two plausible-sounding shortcuts, both of which were tried and
    # rejected on 2026-08-13. Do not re-derive them from first principles.
    #
    # SHORTCUT REJECTED (a): "escalate only when the base date returns zero."
    #   Tempting, because unioning Agoda across 10 dates spanning 40 weeks adds
    #   nothing over the BEST single date (Hilton 18/18, Jumeirah 14/14, Dusit
    #   12/12, Movenpick 9/9 -- union == best, every time).
    #   But the BASE date is routinely NOT the best date. Measured, base vs
    #   full ladder on the same hotels:
    #       Hilton The Walk   14 -> 18  (+4, +29%)
    #       Dusit Thani        9 -> 12  (+3, +33%)
    #       Jumeirah Beach    13 -> 14  (+1)
    #   Gating on zero would have silently dropped those rooms. The ladder is
    #   not searching for MORE than the best date -- it is how we FIND the best
    #   date without knowing in advance which one it is.
    #
    # SHORTCUT REJECTED (b): "stop the ladder once the count stops growing."
    #   The rungs that paid differed per hotel (weekend+2, weekend+3,
    #   weekend+8), so any early exit is a coin flip on which hotel it robs.
    #
    # The honest cost: the ladder is ~90% of Agoda wall-clock (1.7s base vs
    # 16.7s with ladder, per hotel). That is a THROUGHPUT problem to solve in
    # the pacing/concurrency layer, NOT by asking Agoda fewer questions --
    # buying speed here is paid for in rooms that never get their photo fixed.
    got = _escalate(ags, hid, m["agoda_name"], already=rooms)
    if len(got) > len(rooms):
        rooms, source = got, "escalation"
    return rooms, source


def _escalate(s, hid, name, already=None):
    """Widening, UNIONING probe ladder for one thin property.

    EVERY rung runs and every room found is kept -- this does NOT stop at the
    first non-empty result. Verified why that matters: for one hotel all 7 rungs
    returned the identical 2 rooms with supplier_count==1 every time (a genuine
    Agoda inventory ceiling, confirmed not assumed), so early exit would have
    looked identical there -- while a different hotel can easily have a later
    rung expose rooms an earlier one did not.

    The ladder widens along the axes that actually gate an availability-scoped
    grid, cheapest first: more weekends out (inventory is released in waves), a
    longer stay (some rates only exist at 2+ nights), a midweek night (business
    hotels differ from weekend leisure), single occupancy (rooms priced for one).

    A property that survives every rung with supplier_count == 0 every time is
    the one case where zero is believable -- Agoda affirmatively reporting that
    it asked its suppliers, on every date shape askable, and none offered it.
    """
    name = (name or "")[:38]
    base = weekend_checkin(config.STAY_WEEKS_OUT)
    # Weeks reused from the Bookme ladder so both platforms widen along the same
    # calendar, rather than each carrying its own private notion of "further
    # out" that drifts as one is tuned.
    weeks = sorted({w for w, _, _ in config.ROOM_PROBES + config.ROOM_PROBES_ESCALATION
                    if w > config.STAY_WEEKS_OUT})
    ladder = [(f"weekend+{w}", weekend_checkin(w), 1, config.ADULTS)
              for w in weeks]
    ladder += [("2-night", base, 2, config.ADULTS),
               ("3-night", base, 3, config.ADULTS),
               ("midweek", base + datetime.timedelta(
                   days=config.PROBE_MIDWEEK_OFFSET_DAYS), 1, config.ADULTS),
               ("1-adult", base, 1, 1)]

    merged = {r["agoda_room_id"]: r for r in (already or [])}
    base_prop, probed, supplier_zero = None, 0, 0
    # A rung we could not ASK (network, throttle) is not a rung that answered
    # "nothing". Only a ladder where every rung actually ran may be cached as
    # complete, or a transient failure would freeze a partial answer in place
    # and every later run would trust it.
    failed_rungs = 0
    for label, ci, nights, adults in ladder:
        co = ci + datetime.timedelta(days=nights)
        try:
            p = agoda.property_rooms(s, hid, ci.isoformat(), co.isoformat(),
                                     adults=adults)
        except Exception as e:
            print(f"    {name}: {label} failed ({type(e).__name__})")
            failed_rungs += 1
            continue
        if not p:
            continue
        probed += 1
        base_prop = base_prop or p
        supplier_zero += 1 if p.get("supplier_count") == 0 else 0
        gained = 0
        for r in p["rooms"]:
            rid = r["agoda_room_id"]
            # Richer record wins on a repeat sighting; a room's best image set
            # is never overwritten by a thinner one from another probe.
            if rid not in merged or len(r["images"]) > len(merged[rid]["images"]):
                merged[rid] = r
                gained += 1
        if gained:
            print(f"    {name}: +{gained} room(s) via {label} (now {len(merged)})")

    if merged and base_prop:
        _cache_agoda({**(_cached_agoda(hid) or {}), **base_prop,
                      "rooms": list(merged.values()), "source": "escalation",
                      # Only a ladder that ran end to end may short-circuit the
                      # next run -- see agoda_rooms()'s cache check.
                      "ladder_complete": failed_rungs == 0},
                     probe=_probe_tag())
    elif probed and supplier_zero == probed:
        print(f"    {name}: every probe returned supplier_count=0 -- Agoda "
              f"reports no supplier offers this property at all")
    return list(merged.values())


# ---------------------------------------------------------- stage 4: mapping
def map_rooms(bookme_rooms, ag_rooms, cat_ids, agoda_url=None):
    """Decide what gets written for one hotel.

    Returns (to_publish, review_rows, unmatched_rows) -- two SEPARATE result
    lists, because they answer different questions: review_rows is "here is a
    candidate, a human should look at the picture and decide"; unmatched_rows
    is "nothing was even a plausible candidate". Four outcomes:

      mapped     a Bookme room with a confident Agoda counterpart -> row + images
      review     a plausible but unproven counterpart -> row WITHOUT images,
                 plus a review_rooms.csv line with the candidate image to eyeball
                 (see config.REVIEW_BAND_CREATES_ROOM)
      unmatched  a Bookme room with no counterpart -> row without images, so the
                 supplier's real inventory is still present in the database, plus
                 an unmatched_rooms.csv line
      agoda-only a room only Agoda knows about -> row under Agoda's own name,
                 with images; there is no Bookme name to use
    """
    publish, review, unmatched = [], [], []

    def _review_row(name, ag, sc):
        # candidate_images carries the FULL gallery (pipe-joined; index 0 is
        # the thumbnail, same convention _room()/mirror_all_images use), and
        # candidate_size_sqft the candidate's own size -- both source URLs,
        # not yet COS-hosted. Storing them here means an approved row can be
        # applied later straight from this CSV, with no re-match, no re-run
        # of the city/Agoda search, and (cache permitting) no network call
        # at all beyond re-hosting the images -- see apply_review_decisions.
        images = ag.get("images") or []
        return {"bookme_room_name": name, "agoda_room_name": ag["room_name"],
                "score": sc, "veto_reason": "",
                "candidate_images": "|".join(images),
                "candidate_size_sqft": ag.get("size_sqft"),
                "agoda_url": agoda_url or "", "decision": ""}

    # ---- score every viable pair ONCE -------------------------------------
    # Nothing here may depend on the order either platform happened to list
    # its rooms in. room_score() uses token_set_ratio, which rates a SUBSET as
    # a perfect 100 -- "Junior Suite" scores 100 against both "Junior Suite"
    # and "Junior Suite Deluxe" -- so exact ties are common, not exotic. The
    # tie-break is therefore real evidence: exact normalised-name equality
    # first, then the smaller length gap (fewer unexplained extra words).
    pairs, best_seen, best_vetoed = [], {}, {}
    corroborated, soft_rescues = {}, {}
    for bi, bm in enumerate(bookme_rooms):
        bm_norm = match.norm_room(bm["room_name"])
        for ai, ag in enumerate(ag_rooms):
            sc, veto, _ = match.room_match(bm["room_name"], ag["room_name"])
            cor = match.corroborations(bm["room_name"], ag["room_name"])
            if veto and not (config.ROOM_SOFT_VETO_RESCUE
                             and match.is_soft_veto(veto) and cor):
                # kept by SCORE, not arrival order, so an unmatched row that
                # falls back to this reports the CLOSEST vetoed candidate and
                # its own reason -- never a veto borrowed from some other,
                # unrelated Agoda room that happened to be compared first
                if bi not in best_vetoed or sc > best_vetoed[bi][0]:
                    best_vetoed[bi] = (sc, ai, veto)
                continue
            # A SOFT veto (tier/view -- a quality LABEL, not the physical room)
            # survives only when something else positively corroborates the
            # pair. See match.is_soft_veto for why that trade is right here:
            # the alternative to this match is not "no photo", it is the
            # hotel-level landmark photo this project exists to remove.
            soft_rescued = bool(veto)
            ag_norm = match.norm_room(ag["room_name"])
            key = (sc, ag_norm == bm_norm, -abs(len(ag_norm) - len(bm_norm)))
            # remembered regardless of whether it wins an assignment, purely so
            # an unmatched row can report what the closest thing actually was
            if bi not in best_seen or key > best_seen[bi][0]:
                best_seen[bi] = (key, sc, ai)
            if sc >= config.ROOM_REVIEW:      # below this it is not a pairing
                pairs.append((key, sc, bi, ai))
                # Recorded per PAIR, so the emit stage below can ask "was THIS
                # assignment corroborated" rather than re-deriving it.
                corroborated[(bi, ai)] = cor
                if soft_rescued:
                    soft_rescues[(bi, ai)] = veto

    # ---- assign globally, best evidence first ------------------------------
    # Previously each Bookme room, in list order, grabbed its own best Agoda
    # room -- so whichever Bookme room came FIRST won a contested Agoda room
    # even when a later Bookme room matched it far better. Same defect class as
    # the tie-break above, one level up: an outcome decided by array position
    # rather than by evidence. Sorting all candidate pairs by strength and
    # assigning greedily removes position from the decision entirely; the
    # trailing indices only make ties deterministic, never preferential.
    pairs.sort(key=lambda p: (p[0][0], p[0][1], p[0][2], -p[2], -p[3]),
               reverse=True)
    partner, taken_ag = {}, set()
    for _key, sc, bi, ai in pairs:
        if bi in partner or ai in taken_ag:
            continue
        partner[bi] = (sc, ai)
        taken_ag.add(ai)

    # ---- emit --------------------------------------------------------------
    for bi, bm in enumerate(bookme_rooms):
        name = bm["room_name"]
        got = partner.get(bi)
        # CORROBORATION, not a lower threshold. Measured over 11 operator-
        # labelled pairs, the should-map and should-not-map sets overlap on
        # score (62.3-74.9 vs 62.5-72.0), so no threshold separates them --
        # every should-map pair has an attribute in positive agreement and
        # every should-not-map pair has none. See match.corroborations.
        if got and got[0] < config.ROOM_ACCEPT and corroborated.get((bi, got[1])):
            ag = ag_rooms[got[1]]
            publish.append(_room(name, ag["images"], cat_ids,
                                 size_sqft=ag.get("size_sqft")))
        elif got and got[0] >= config.ROOM_ACCEPT:
            ag = ag_rooms[got[1]]
            publish.append(_room(name, ag["images"], cat_ids,
                                 size_sqft=ag.get("size_sqft")))
        elif got:                              # ROOM_REVIEW <= score < ACCEPT
            ag = ag_rooms[got[1]]
            # The Agoda room stays claimed (it is in taken_ag) even though its
            # images are withheld: republishing it under its own name would put
            # two near-identical rooms live for what is probably one room,
            # which is worse than the gap the review CSV already records.
            review.append(_review_row(name, ag, got[0]))
            if config.REVIEW_BAND_CREATES_ROOM:
                # Size withheld along with the images, same reasoning: this
                # pairing is not yet trusted, so neither of the Agoda room's
                # facts should land on the Bookme room's row until a human
                # confirms it really is the same physical room.
                publish.append(_room(name, [], cat_ids))
        else:
            # bi did not win the primary assignment -- either nothing
            # plausible existed, or a rival Bookme room with equal/stronger
            # evidence claimed the same Agoda room first. Losing that race is
            # NOT itself evidence against bi: bi's own best candidate already
            # passed every veto (class, tiers, bedrooms, view, beds all agree
            # wherever both sides state them), so if ITS OWN score clears the
            # normal accept/review bar, it is handled on that merit alone --
            # contention between two plausible Bookme names for one physical
            # room (rate-plan duplicates are common on Bookme's side) must
            # not silently downgrade a confident match to "nothing found".
            near = best_seen.get(bi)
            if (near and near[1] < config.ROOM_ACCEPT
                    and corroborated.get((bi, near[2]))):
                ag = ag_rooms[near[2]]
                publish.append(_room(name, ag["images"], cat_ids,
                                     size_sqft=ag.get("size_sqft")))
            elif near and near[1] >= config.ROOM_ACCEPT:
                ag = ag_rooms[near[2]]
                publish.append(_room(name, ag["images"], cat_ids,
                                     size_sqft=ag.get("size_sqft")))
            elif near and near[1] >= config.ROOM_REVIEW:
                ag = ag_rooms[near[2]]
                review.append(_review_row(name, ag, near[1]))
                if config.REVIEW_BAND_CREATES_ROOM:
                    publish.append(_room(name, [], cat_ids))
            else:
                # truly nothing plausible: either every candidate was vetoed
                # (report the closest one and ITS OWN reason -- never one
                # borrowed from a different, unrelated Agoda room) or no
                # candidate cleared even the review floor, or there were no
                # Agoda rooms to compare against at all
                vetoed = best_vetoed.get(bi)
                best_name = ag_rooms[vetoed[1]]["room_name"] if vetoed else ""
                best_score = vetoed[0] if vetoed else ""
                reason = vetoed[2] if vetoed else ""
                unmatched.append({"bookme_room_name": name, "best_agoda_room": best_name,
                                  "score": best_score, "veto_reason": reason})
                publish.append(_room(name, [], cat_ids))

    for ai, ag in enumerate(ag_rooms):
        if ai not in taken_ag and ag["room_name"]:
            publish.append(_room(ag["room_name"], ag["images"], cat_ids,
                                 size_sqft=ag.get("size_sqft")))
    return publish, review, unmatched


# ------------------------------------------------ stage 4b: booking gap-fill
def booking_shapes(shapes=None, today=None):
    """config.BOOKING_PROBES -> [(check_in, check_out)] real dates."""
    out = []
    for weeks, nights in (shapes or config.BOOKING_PROBES):
        ci = weekend_checkin(weeks_out=weeks, today=today)
        out.append((ci, ci + datetime.timedelta(days=nights)))
    return out


def _is_gap(room, existing):
    """A room worth asking a second source about.

    Not merely "no candidate images this run": a room the DATABASE already has
    a picture for is not a gap, because db.publish()'s COALESCE only ever fills
    an empty field, so anything fetched for it would be discarded on write. The
    key derivation must match `_split_for_mirroring` exactly -- if the two
    disagree, this pass fetches for rooms that stage then throws away.
    """
    if room["source_images"]:
        return False
    key = (room["name"] or "").strip()[:config.ROOM_NAME_MAX].lower()
    prior = (existing or {}).get(key)
    return not (prior and prior["has_image"])


def _booking_unfilled(gap_names, bk_rooms, cat_ids):
    """True if at least one of the caller's gaps still has no matched,
    photographed Booking counterpart -- the adaptive escalation's stop check.

    Calls the REAL matcher (`map_rooms`), not a name-equality shortcut: a room
    that merely SHARES a name is not evidence it is the same physical room, and
    the whole point of routing through `map_rooms` is that its vetoes already
    settle that question. This is cheap to call repeatedly -- both lists are a
    handful of rooms, no network, pure comparison.
    """
    photographed = [r for r in bk_rooms if r["images"]]
    if not photographed:
        return True
    filled, _review, _unmatched = map_rooms(gap_names, photographed, cat_ids)
    return any(not r["source_images"] for r in filled)


def booking_fill(bs, hotel, to_publish, cat_ids, city="", existing=None,
                 log=None):
    """Fill imageless rooms in `to_publish` from Booking.com, in place.

    Runs ONLY on the rooms Agoda left empty, and only for hotels that have
    such rooms -- a hotel Agoda covered fully costs nothing here. Returns
    (n_filled, note, filled_room_names). `note` explains a zero, because
    "Booking added nothing" has four very different causes that must not be
    reported alike: no gaps to fill, no verified identity, no rooms on any
    probed date, or rooms that existed but matched none of the gaps. The names
    are carried out so QA can check the actual pairings rather than a count.

    It never INVENTS a room. Booking room types with no Bookme counterpart are
    dropped rather than published: Agoda's leftovers are published under their
    own names because that path is measured and matched, whereas a Booking
    listing may legitimately be one apartment inside the building (see
    resolve_verified's accepted risk) whose room set is not the hotel's. Filling
    a room Bookme already sells cannot introduce a room that does not exist;
    adding one can.
    """
    log = log or (lambda *_: None)
    gaps = [r for r in to_publish if _is_gap(r, existing)]
    if not gaps:
        return 0, "no gaps", []
    try:
        cand = booking.resolve_verified(
            bs, hotel, city=city, max_km=config.BOOKING_MAX_KM,
            geo_candidates=config.BOOKING_GEO_CANDIDATES,
            min_name=config.BOOKING_MIN_NAME)
    except booking.Blocked as e:
        return 0, f"blocked: {e}", []
    if not cand:
        return 0, "no geo-verified candidate", []
    seed = cand.pop("_html", None)          # transient; never cached, never stored

    def probe(shapes, seed_html=None):
        return booking.rooms_union(bs, cand["slug"], cand["country"], shapes,
                                   seed_html=seed_html, adults=config.ADULTS)

    gap_names = [{"room_name": r["name"]} for r in gaps]
    shapes_tried = 1 + len(config.BOOKING_PROBES)   # +1 for the free seed page
    try:
        # BASE: run in full, unconditionally -- measured 2026-08-17 across 10
        # geo-verified Dubai hotels, `config.BOOKING_PROBES`'s two shapes
        # captured 94% (90/96) of every photographed room found across an
        # 8-shape sweep. Cheap and reliably valuable, so there is no reason to
        # gate it behind a zero check the way escalation is gated.
        bk_rooms = probe(booking_shapes(), seed_html=seed)

        # ESCALATE ONE SHAPE AT A TIME, stopping the moment it stops paying.
        # A resolved property still short of the caller's gaps is an OPEN
        # QUESTION, not an answer -- the grid is availability-scoped, so it is
        # equally consistent with "closed on these nights". But the same
        # measurement that justified the base shapes also bounds how far to
        # chase this: shapes 3-4 in order added +3 and +2 (across 10 hotels),
        # and every shape past that added +1 or 0. Escalating the WHOLE list
        # unconditionally would spend most of its cost on shapes that
        # essentially never pay -- so shapes run one at a time, ordered
        # strongest-marginal-value first (config.BOOKING_PROBES_ESCALATION),
        # and stop as soon as either the gaps are fully covered or
        # BOOKING_ESCALATION_STOP consecutive shapes add no new photographed
        # room. This is the quality/speed balance point measured, not guessed.
        if _booking_unfilled(gap_names, bk_rooms, cat_ids):
            flat = 0
            for shape in config.BOOKING_PROBES_ESCALATION:
                before = {r["booking_room_id"] for r in bk_rooms if r["images"]}
                bk_rooms = booking.merge_rooms(bk_rooms, probe(booking_shapes([shape])))
                shapes_tried += 1
                gained = {r["booking_room_id"] for r in bk_rooms
                         if r["images"]} - before
                flat = 0 if gained else flat + 1
                if flat >= config.BOOKING_ESCALATION_STOP:
                    break
                if not _booking_unfilled(gap_names, bk_rooms, cat_ids):
                    break
    except booking.Blocked as e:
        return 0, f"blocked: {e}", []
    except (requests.RequestException, RuntimeError) as e:
        return 0, f"fetch failed: {e}", []
    bk_rooms = [r for r in bk_rooms if r["images"]]
    if not bk_rooms:
        return 0, (f"{cand['slug']}: no photographed rooms on any of "
                   f"{shapes_tried} date shapes"), []

    # Matching is delegated to map_rooms rather than reimplemented: it holds the
    # veto rules, the review/accept bars and the position-independent global
    # assignment, all of which are selftested. Duplicating that here would be a
    # second copy free to drift -- and a room mislabelled by the copy is this
    # project's exact defect.
    # The review-band candidates are deliberately DROPPED rather than added to
    # rooms_review.csv: that file has no source column, so a Booking candidate
    # would arrive indistinguishable from an Agoda one, with an empty agoda_url
    # and Booking image urls -- a reviewer could not tell what they were
    # approving. This is a real, known coverage loss (a 62-75 Booking pairing
    # that a human might have accepted), recoverable by adding a `source`
    # column to REVIEW_COLUMNS; it is not an oversight.
    filled_rows, _review_dropped, _unmatched = map_rooms(gap_names, bk_rooms,
                                                         cat_ids)
    # Restricted to the gap names, and FIRST occurrence wins. map_rooms emits
    # the Bookme-derived rows before any source-only leftovers, so a Booking
    # room that merely shares a name with a matched one cannot overwrite the
    # match -- a plain dict comprehension over every row would let it, silently,
    # and which imagery won would depend on emission order rather than evidence.
    gap_names_set = {r["name"] for r in gaps}
    by_name = {}
    for r in filled_rows:
        if r["source_images"] and r["name"] in gap_names_set:
            by_name.setdefault(r["name"], r)
    n, names = 0, []
    for r in gaps:
        got = by_name.get(r["name"])
        if not got:
            continue
        r["source_images"] = got["source_images"]
        names.append(r["name"])
        # size_sqft is left alone: Booking states room size far less often than
        # Agoda and in mixed units, and a wrong size is a visible lie on a
        # product page. Images are what this pass exists to supply.
        r["image_source"] = "booking"
        n += 1
        log(f"      + {r['name'][:44]:46} {len(r['source_images'])} imgs "
            f"(booking:{cand['slug'][:28]})")
    if not n:
        return 0, (f"{cand['slug']}: {len(bk_rooms)} rooms, none matched the "
                   f"{len(gaps)} gap(s)"), []
    return n, cand["slug"], names


def _validated_cat_ids(conn, what="category sync"):
    """categories.resolve(), plus the guarantee `_room()` depends on: cat_ids
    covers every name classify() can ever return.

    classify() only ever returns a name from categories.ALL, so a COMPLETE
    cat_ids can never miss a lookup -- `_room()`'s fallback-to-General path
    should be unreachable in practice, dead code for a cat_ids that broke in
    some new way tomorrow. This is what makes that promise true TODAY: an
    incomplete cat_ids is refused here, before a single hotel is processed,
    rather than silently reaching _room() and writing NULL category_id across
    however many hundred rooms happen to run before anyone notices.

    Root-caused from a real incident: a batch of live runs wrote 1,000+ rooms
    with room_category_id NULL despite every category already existing in
    v2_room_categories -- proven, by reproducing the exact write path in
    isolation, that _room()/db.publish() write categories correctly whenever
    cat_ids is complete. The only way that incident's symptom is possible is a
    cat_ids that was incomplete or empty at the time, most likely from a
    concurrently-edited copy of this code on disk. This function is the fix
    for the CLASS: whatever produces a broken cat_ids next, it gets caught
    here instead of silently reaching every room in the run.
    """
    (cat_ids, created), conn = db.with_retry(conn, db.sync_categories, what=what)
    missing = [n for n in categories.ALL if not cat_ids.get(n)]
    if missing:
        raise SystemExit(
            f"category sync returned an INCOMPLETE mapping -- missing "
            f"{missing!r} of {len(categories.ALL)} categories. Refusing to "
            f"process any hotel with this cat_ids: every room in the run "
            f"would silently write NULL. This means v2_room_categories "
            f"itself is missing a category, or categories.resolve() failed "
            f"partway -- fix that before re-running, not this guard.")
    return cat_ids, created, conn


def _room(name, images, cat_ids, size_sqft=None):
    cat = categories.classify(name)
    cat_id = cat_ids.get(cat)
    if cat_id is None:
        # Should be unreachable given _validated_cat_ids() -- classify() only
        # returns names from categories.ALL, and that function refuses to
        # proceed unless cat_ids covers all of them. If this fires anyway,
        # something upstream skipped the guard; fall back to General rather
        # than let a NULL reach the database, and say so loudly rather than
        # silently -- a quiet fallback here is exactly how the original
        # incident went unnoticed for hours.
        print(f"WARNING: category {cat!r} (for room {name!r}) is not in "
              f"cat_ids -- falling back to {categories.FALLBACK!r}. This "
              f"should be impossible; cat_ids was supposed to be validated "
              f"complete before this call.")
        cat_id = cat_ids.get(categories.FALLBACK)
    imgs = images[:config.MAX_IMAGES_PER_ROOM]
    # Provenance, so "where did this photo come from" is answerable from the
    # run report without re-deriving it. "" means the room has no imagery yet --
    # booking_fill() overwrites it for the rooms it rescues.
    return {"name": name, "category": cat, "category_id": cat_id,
            "size_sqft": size_sqft, "source_images": imgs,
            "image_source": "agoda" if imgs else ""}


IMAGE_WORKERS = 8


def _split_for_mirroring(to_publish, existing):
    """Which candidate rooms are worth downloading+re-uploading images for.

    A room already carrying a thumbnail in the database gets NOTHING from
    re-fetching the same (or another) candidate photo -- db.publish()'s
    COALESCE only ever fills an EMPTY field, so the mirrored bytes would be
    thrown away on write regardless. Filtering here, before the network calls,
    is the fix; filtering only at write time (the old behavior) still paid for
    every download and upload first.

    size_sqft is never a reason to route a room through here: it comes
    straight from the matched Agoda candidate's own field, no network round
    trip involved, and db.publish() backfills it independently of whether a
    room's images were touched this run.

    Returns (need_mirror, already_imaged). `already_imaged` rooms have their
    `thumbnail`/`images` explicitly set to the empty state (never mirrored,
    never claimed to have been), matching the shape `mirror_all_images` would
    have produced for a room it intentionally skipped.
    """
    need_mirror, already_imaged = [], []
    for r in to_publish:
        key = (r["name"] or "").strip()[:config.ROOM_NAME_MAX].lower()
        prior = existing.get(key)
        if prior and prior["has_image"]:
            r["thumbnail"], r["images"] = None, []
            already_imaged.append(r)
        else:
            need_mirror.append(r)
    return need_mirror, already_imaged


def mirror_all_images(rooms, session, max_workers=IMAGE_WORKERS):
    """Download + re-host every room's images for one hotel, CONCURRENTLY.

    Image 1 of each room becomes its thumbnail, the rest become attachments --
    together the complete gallery, with the lead image not duplicated across
    the two surfaces.

    Every image round-trips through TWO services that are neither Agoda's
    page/search API nor each other: the source is Agoda's/bstatic's image CDN,
    the destination is Tencent COS. Neither is what config.MIN_INTERVAL paces
    (that pacer exists specifically because Agoda's PAGE api blocks bursts --
    see agoda.py; it is process-global, so it would gate these calls too if
    they went through it, but plain CDN/object-storage GETs and PUTs never do)
    -- so there is no rate-limit reason to fetch them one at a time, and doing
    so was pure wasted wall-clock time. A hotel with 5 rooms x 6 images was
    30 sequential download+upload round trips; this makes it up to
    IMAGE_WORKERS concurrent ones instead.

    Order within a room is preserved even though completion order is not:
    ThreadPoolExecutor.map() returns results in the same order as its input
    iterable regardless of which thread finishes first, so results are
    re-associated by position, never by arrival.
    """
    jobs = [(ri, url) for ri, r in enumerate(rooms) for url in r["source_images"]]
    if not jobs:
        for r in rooms:
            r["thumbnail"], r["images"] = None, []
        return 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        results = list(ex.map(lambda j: cos.mirror(j[1], session=session), jobs))
    by_room = [[] for _ in rooms]
    for (ri, _url), cos_url in zip(jobs, results):
        by_room[ri].append(cos_url)
    uploaded = 0
    for ri, r in enumerate(rooms):
        urls = [u for u in by_room[ri] if u]
        r["thumbnail"] = urls[0] if urls else None
        r["images"] = urls[1:]
        uploaded += len(urls)
    return uploaded


# ------------------------------------------------------------------- reports
HOTEL_COLUMNS = ["run_id", "city_id", "hotel_id", "slug", "name_en", "reason",
                 "detail", "attempted_probes"]
_ROOM_ID_COLUMNS = ["run_id", "city_id", "city_name", "hotel_id", "slug", "hotel_name"]
REVIEW_COLUMNS = ["row_id"] + _ROOM_ID_COLUMNS + [
    "bookme_room_name", "agoda_room_name", "score", "veto_reason",
    "candidate_images", "candidate_size_sqft", "agoda_url", "decision"]
UNMATCHED_COLUMNS = ["row_id"] + _ROOM_ID_COLUMNS + [
    "bookme_room_name", "best_agoda_room", "score", "veto_reason"]
BOOKING_COLUMNS = ["run_id", "hotel_id", "hotel_name", "gaps", "filled",
                   "outcome", "rooms_filled"]


def _revisit_row(rows, run_id, h, probes, reason, detail=""):
    """One line of hotels_to_revisit.csv -- a hotel this run could not finish,
    recorded with WHY so it can be re-run rather than silently lost."""
    rows.append({"run_id": run_id, "city_id": h["city_id"], "hotel_id": h["id"],
                 "slug": h["slug"], "name_en": h["name"], "reason": reason,
                 "detail": str(detail)[:300], "attempted_probes": probes})


def _write_csv(path, columns, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        # restval="" so a row missing an optional column (e.g. unmatched rows
        # have no `decision` field) writes a blank cell instead of raising.
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore", restval="")
        w.writeheader()
        w.writerows(rows)


# ------------------------------------------------- stage 5: review retrigger
APPROVED_DECISIONS = {"y", "yes", "approve", "approved"}


def _approved(rows):
    return [r for r in rows
            if (r.get("decision") or "").strip().lower() in APPROVED_DECISIONS]


def _room_from_review_row(r, cat_ids):
    """A rooms_review.csv row -> a _room()-shaped dict. Pure and offline --
    the one piece of apply_review_decisions unit-testable without a live DB
    or COS connection (see selftest; db.py and cos.py's own __main__ carry
    the live proof of the write path this hands off to)."""
    images = [u for u in (r.get("candidate_images") or "").split("|") if u]
    size = r.get("candidate_size_sqft") or None
    return _room(r["bookme_room_name"], images, cat_ids,
                size_sqft=int(size) if size else None)


def apply_review_decisions(csv_path, conn, cat_ids, session=None, dry_run=False):
    """Apply human decisions from a rooms_review.csv -- no re-match, no
    re-run of the city/Agoda search. The candidate images and size were
    already captured in the CSV at match time (see _review_row in
    map_rooms), so this costs nothing upstream: only re-hosting the approved
    images to COS, then the SAME additive-only db.publish() every other room
    in the pipeline goes through -- reused as-is, not reimplemented.

    A row is applied when `decision` (case-insensitive) is in
    APPROVED_DECISIONS; everything else (blank, "no", anything else) is left
    untouched, so a partially-reviewed file can be re-applied later without
    reprocessing rows nobody has decided on yet. A row that applies
    successfully is then REMOVED from csv_path -- it is a work queue, not a
    log, so a resolved row does not need to keep showing up as pending -- by
    `row_id`, so a row without one (an older CSV, from before that column
    existed) is applied but left in place rather than risk deleting the
    wrong row. Re-applying an already-removed row is moot; re-applying one
    that is somehow still present is a safe no-op regardless, because
    db.publish() is the same COALESCE-guarded write path a normal run uses.
    """
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)
    approved = _approved(rows)
    print(f"{csv_path}: {len(rows)} row(s), {len(approved)} approved")

    applied = failed = 0
    done_ids = set()
    for r in approved:
        room = _room_from_review_row(r, cat_ids)
        label = f"{r.get('hotel_name', '?')!r} / {room['name']!r}"
        if dry_run:
            print(f"  [dry-run] would apply {label} "
                  f"({len(room['source_images'])} image(s))")
            applied += 1
            continue
        mirror_all_images([room], session)
        if room["source_images"] and not room["thumbnail"]:
            print(f"  {label}: every candidate image failed to mirror, skipped")
            failed += 1
            continue
        n_rooms, n_att, skipped, _dup, backfilled = db.publish(
            conn, {"id": int(r["hotel_id"])}, [room])
        status = "inserted" if n_rooms else "backfilled" if backfilled else "already complete"
        print(f"  {label}: {status}, {n_att} image(s) attached")
        applied += 1
        if r.get("row_id"):
            done_ids.add(r["row_id"])
        else:
            print(f"  {label}: no row_id on this row (older CSV format) -- "
                  f"applied, but left in {csv_path}, remove it by hand")

    print(f"applied {applied}, failed {failed}, "
          f"not approved {len(rows) - len(approved)}")
    if done_ids:
        remaining = [r for r in rows if r.get("row_id") not in done_ids]
        _write_csv(csv_path, fieldnames, remaining)
        print(f"removed {len(done_ids)} applied row(s) from {csv_path}, "
              f"{len(remaining)} left pending")
    return applied, failed


# ---------------------------------------------------------------- selftest
def selftest():
    """Offline checks on the logic that decides what gets written: how a hotel's
    Bookme rooms are harvested, and which room gets which pictures. Both fail
    silently and expensively in production if wrong."""
    # -- probe ladder ------------------------------------------------------
    # Every shape must produce a weekend AND a midweek variant, and check_out
    # must respect the shape's night count -- a ladder that silently probed one
    # night everywhere would look identical in the logs while asking a strictly
    # narrower question.
    shapes = probe_shapes([(1, 2, 1), (2, 1, 3)])
    assert len(shapes) == 4, shapes
    ci1, co1, ad1, _ = shapes[0]
    ci2 = shapes[1][0]
    assert (co1 - ci1).days == 1 and ad1 == 2
    assert ci2 - ci1 == datetime.timedelta(days=config.PROBE_MIDWEEK_OFFSET_DAYS), \
        "the midweek variant is not offset from its weekend"
    assert (shapes[2][1] - shapes[2][0]).days == 3, "night count not honoured"
    assert shapes[2][2] == 1, "occupancy not honoured"
    assert len({s[3] for s in shapes}) == 4, "probe labels are not distinct"
    # the ladders must not overlap, or escalation re-asks what base already did
    assert not (set(config.ROOM_PROBES) & set(config.ROOM_PROBES_ESCALATION)), \
        "escalation repeats a base shape instead of widening"

    # -- harvest: union, and a zero is never accepted as-is ----------------
    # Every harvest below check-points to disk, so CACHE is redirected at a
    # temp dir for the whole section: a selftest must not read or write the
    # real run's checkpoint, and must not depend on whether one exists.
    import tempfile as _tf
    real_cache, real_probe = CACHE, PROBE_CACHE
    _tmpdir = _tf.mkdtemp()
    globals()["CACHE"] = _tmpdir
    globals()["PROBE_CACHE"] = os.path.join(_tmpdir, "bookme_rooms.json")
    try:
        _selftest_harvest()
    finally:
        globals()["CACHE"], globals()["PROBE_CACHE"] = real_cache, real_probe
        shutil.rmtree(_tmpdir, ignore_errors=True)

    cats = {n: i for i, n in enumerate(categories.ALL, 1)}
    _selftest_mapping(cats)
    _selftest_booking_fill(cats)
    _selftest_agoda_breaker()
    _selftest_plan_checkpoint(cats)
    _selftest_cli_targeting()


def _selftest_plan_checkpoint(cats):
    """The Phase 1/Phase 2 handoff: what gets written, what a reload sees, and
    the two ways a stale checkpoint must be refused rather than trusted.

    Redirects CACHE/PLAN_CACHE to a temp dir for the whole section, same
    reasoning as _selftest_harvest: this must not read or write the real run's
    checkpoint, and must not depend on whether one exists.
    """
    import tempfile as _tf
    real_cache, real_plan = CACHE, PLAN_CACHE
    _tmpdir = _tf.mkdtemp()
    globals()["CACHE"] = _tmpdir
    globals()["PLAN_CACHE"] = os.path.join(_tmpdir, "plan.csv")
    try:
        h1 = {"id": 501, "city_id": 1280, "city_name": "Dubai",
             "slug": "hotel-a", "name": "Hotel A"}
        rooms1 = [_room("King Room", ["https://agoda/k.jpg"], cats, size_sqft=280),
                  _room("Twin Room", [], cats)]
        _append_plan_rows(h1, rooms1, "5", 2, "http", 1, 0)

        h2 = {"id": 502, "city_id": 1280, "city_name": "Dubai",
             "slug": "hotel-b", "name": "Hotel B"}
        rooms2 = [_room("Suite", ["https://b/1.jpg", "https://b/2.jpg"], cats)]
        rooms2[0]["image_source"] = "booking"          # not agoda's own default
        _append_plan_rows(h2, rooms2, "3", 0, "no_agoda_match", 0, 0)

        scope = _plan_scope([h1, h2])
        _save_plan_scope(scope)

        # -- round trip: what got written is what comes back -------------------
        by_hotel, planned_ids = _load_plan(scope)
        assert planned_ids == {501, 502}, planned_ids
        assert len(by_hotel[501]) == 2 and len(by_hotel[502]) == 1, by_hotel

        r1 = [_room_from_plan_row(r, cats) for r in by_hotel[501]]
        by_name = {r["name"]: r for r in r1}
        assert by_name["King Room"]["source_images"] == ["https://agoda/k.jpg"], by_name
        assert by_name["King Room"]["size_sqft"] == 280, by_name["King Room"]
        assert by_name["King Room"]["image_source"] == "agoda", by_name["King Room"]
        assert by_name["Twin Room"]["source_images"] == [], by_name["Twin Room"]

        r2 = [_room_from_plan_row(r, cats) for r in by_hotel[502]]
        assert r2[0]["source_images"] == ["https://b/1.jpg", "https://b/2.jpg"], r2
        # THE CONTRACT THAT MATTERS HERE: provenance survives the round trip.
        # _room()'s own default would call any non-empty image list "agoda" --
        # a plan row must override that with what was actually recorded, or a
        # coverage report reads every booking-sourced room as an agoda one.
        assert r2[0]["image_source"] == "booking", (
            f"image_source was not preserved across the plan round trip: {r2[0]}")

        # -- resumability: an already-planned hotel is skippable ---------------
        # This is the exact filter Phase 1 applies to `work1` -- asserted
        # directly rather than by re-running all of main().
        todo = [h1, h2, {"id": 503, "city_id": 1280, "slug": "hotel-c", "name": "Hotel C"}]
        work1 = [h for h in todo if h["id"] not in planned_ids]
        assert [h["id"] for h in work1] == [503], (
            f"a hotel already check-pointed was re-offered to discovery: {work1}")

        # -- scope invalidation: a DIFFERENT hotel set must not reuse this plan
        other_scope = _plan_scope([h1])              # h2 missing -> different scope
        assert other_scope != scope, "scope is not sensitive to the hotel set"
        by_hotel2, planned_ids2 = _load_plan(other_scope)
        assert by_hotel2 == {} and planned_ids2 == set(), (
            "a plan for a different hotel set was accepted as current")

        # -- scope invalidation: matching TUNING must also invalidate --------
        # A tightened Booking gate or review threshold changed which rooms
        # WOULD have been matched; reusing rooms matched under the old rules
        # would silently publish a decision the current config disagrees with.
        real_min_name = config.BOOKING_MIN_NAME
        config.BOOKING_MIN_NAME = real_min_name + 1
        try:
            tuning_scope = _plan_scope([h1, h2])
        finally:
            config.BOOKING_MIN_NAME = real_min_name
        assert tuning_scope != scope, (
            "plan scope is blind to matching config, not just the hotel set")

        # -- a torn last row is dropped, not fatal, same tolerance as ledger.py
        with open(PLAN_CACHE, "a", encoding="utf-8") as f:
            f.write("1280,Dubai,999,torn-slug,Torn Hote")   # short row, no newline
        by_hotel3, planned_ids3 = _load_plan(scope)
        assert 501 in by_hotel3 and 502 in by_hotel3, (
            "a torn trailing row lost the good ones")
        assert 999 not in planned_ids3, "a torn row was accepted as a real hotel"
    finally:
        globals()["CACHE"], globals()["PLAN_CACHE"] = real_cache, real_plan
        shutil.rmtree(_tmpdir, ignore_errors=True)
    print("OK: plan checkpoint round-trips rooms (images, size, category, "
          "provenance), skips already-planned hotels on resume, and is "
          "invalidated by a different hotel set OR a changed matching config")
    _selftest_phase2_resume()


def _selftest_phase2_resume():
    """The other half of resumability: Phase 2 must not RE-WALK a hotel an
    earlier attempt already finished, against a REAL Ledger (not a stand-in),
    so this proves the actual `led.fresh_ids()`/`unresolved_ids()` shapes
    _phase2_skip_ids() depends on, not an assumption about them.

    Three hotels, three different real states a resumed Phase 2 must tell
    apart -- this is exactly the CPO-facing requirement: re-invoking after a
    crash must skip what's genuinely done, keep what still owes a picture,
    and never mistake one for the other.
    """
    import tempfile as _tf
    real_pub, real_unres = ledger.PUBLISHED_PATH, ledger.UNRESOLVED_PATH
    _tmpdir = _tf.mkdtemp()
    ledger.PUBLISHED_PATH = os.path.join(_tmpdir, "p.csv")
    ledger.UNRESOLVED_PATH = os.path.join(_tmpdir, "u.csv")
    try:
        led = ledger.open_ledger()
        done = {"id": 701, "city_id": 1280, "slug": "done-hotel", "name": "Done"}
        needs_pic = {"id": 702, "city_id": 1280, "slug": "needs-pic", "name": "Needs Pic"}
        # id 703 is never published at all -- must also survive (not in
        # skip_ids just because it was never touched)

        led.mark_published(done, "run1", 5, 5)
        led.mark_published(needs_pic, "run1", 3, 1)
        led.mark_unresolved(needs_pic, "run1", "needs_image_backfill",
                            "2 room(s) published without a thumbnail this run")

        skip = _phase2_skip_ids(led)
        assert 701 in skip, "a fully-published, fully-resolved hotel was NOT skipped"
        assert 702 not in skip, (
            "a hotel still owing an image backfill was skipped as if finished -- "
            "a resumed Phase 2 would silently leave it incomplete forever")
        assert 703 not in skip, "a never-attempted hotel was skipped"

        # a fresh Ledger() reload (simulating the NEXT process) must agree
        led2 = ledger.open_ledger()
        assert _phase2_skip_ids(led2) == skip, "skip set did not survive a reload"
    finally:
        ledger.PUBLISHED_PATH, ledger.UNRESOLVED_PATH = real_pub, real_unres
        shutil.rmtree(_tmpdir, ignore_errors=True)
    print("OK: a resumed Phase 2 skips hotels already published AND resolved, "
          "keeps ones still owing an image backfill, and survives a reload")


def _selftest_cli_targeting():
    """--slugs/--slugs-file parsing and the wizard's offset/range parsing --
    both pure, both offline, both exercised directly rather than only through
    a live CLI invocation."""
    import tempfile as _tf

    # -- slug list: inline, file, union, de-dup, order preserved, comments --
    assert _parse_slug_list("a,b,c", None) == ["a", "b", "c"]
    assert _parse_slug_list(" a , b ,,c ", None) == ["a", "b", "c"], (
        "whitespace/empty entries must not survive")
    assert _parse_slug_list("a,b,a", None) == ["a", "b"], "duplicates must collapse"
    with _tf.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("hotel-x\n# a comment line\n\nhotel-y  # trailing comment\nhotel-x\n")
        path = f.name
    try:
        assert _parse_slug_list(None, path) == ["hotel-x", "hotel-y"], (
            "file parsing must skip blanks/comments and strip trailing comments")
        assert _parse_slug_list("a,hotel-x", path) == ["a", "hotel-x", "hotel-y"], (
            "--slugs and --slugs-file must UNION, not one replacing the other")
    finally:
        os.remove(path)

    # -- wizard bound/range parsing --------------------------------------
    assert _parse_bound_reply("all") == (None, 0)
    assert _parse_bound_reply("  ALL ") == (None, 0), "case/whitespace insensitive"
    assert _parse_bound_reply("25") == (25, 0)
    assert _parse_bound_reply("0") is _BOUND_INVALID, "zero is not a valid count"
    assert _parse_bound_reply("-5") is _BOUND_INVALID
    # THE FEATURE THAT MATTERS: 1-indexed, BOTH ENDS INCLUDED, matching how a
    # person reads the range aloud -- '6-7' is 2 hotels (the 6th and 7th), not
    # 1. Converted to a 0-indexed (count, offset) pair for the actual slice.
    assert _parse_bound_reply("6-7") == (2, 5), (
        f"'6-7' must select 2 hotels (6th and 7th), got {_parse_bound_reply('6-7')}")
    assert _parse_bound_reply("1000-1643") == (644, 999), (
        f"'1000-1643' must select 644 hotels (1000th..1643rd inclusive), "
        f"got {_parse_bound_reply('1000-1643')}")
    assert _parse_bound_reply("1000:1643") == (644, 999), "':' must work like '-'"
    assert _parse_bound_reply(" 10 - 19 ") == (10, 9), "range tolerates spacing"
    assert _parse_bound_reply("6-6") == (1, 5), (
        "equal ends must select exactly that ONE hotel, not be rejected as empty")
    assert _parse_bound_reply("7-6") is _BOUND_INVALID, (
        "a genuinely backwards range (end < start) must be rejected")
    assert _parse_bound_reply("0-5") is _BOUND_INVALID, (
        "position 0 does not exist -- the list is 1-indexed, like the wizard "
        "displays it, not 0-indexed like a Python slice")
    assert _parse_bound_reply("banana") is _BOUND_INVALID

    print("OK: --slugs/--slugs-file union+dedupe+comments, wizard range "
          "parsing ('6-7' -> 2 hotels inclusive, 1-indexed), accepts a "
          "single-hotel 'A-A', rejects position 0 and backwards ranges")


def _selftest_agoda_breaker():
    """The escalating stand-down, with sleep stubbed out.

    Guards a real two-hour production livelock: a FIXED cooldown that resets
    its own counter can never outlast a block longer than itself, so the run
    loops "6 failures -> sleep -> 6 failures -> sleep" indefinitely while
    every hotel is written up as `no agoda match`. Both halves of the fix are
    asserted -- the wait must grow, and one success must reset it.
    """
    real_sleep, slept = time.sleep, []
    real_consec, real_cools = agoda._consecutive, agoda._cooldowns
    try:
        time.sleep = slept.append
        agoda._consecutive = agoda._cooldowns = 0
        for _ in range(3):                        # three full cooldown cycles
            for _ in range(agoda.CONSECUTIVE_LIMIT):
                agoda._note(False, log=lambda *_: None)
        assert len(slept) == 3, slept
        assert slept[1] > slept[0] and slept[2] > slept[1], (
            f"cooldown did not escalate: {slept} -- a fixed wait cannot outlast "
            f"a block that is longer than it")
        assert agoda.health()[0] == 3, agoda.health()

        # ...and a single success must clear it, or a healthy run inherits an
        # hour-long penalty from an outage that already ended.
        agoda._note(True)
        assert agoda.health()[0] == 0, agoda.health()
        assert agoda.health()[1] == agoda.COOLDOWN, agoda.health()

        # the ceiling must actually bind, or a long outage schedules a sleep
        # measured in days
        agoda._cooldowns = 99
        assert agoda.health()[1] == agoda.COOLDOWN_MAX, agoda.health()
    finally:
        time.sleep = real_sleep
        agoda._consecutive, agoda._cooldowns = real_consec, real_cools
    print(f"OK: agoda stand-down escalates {[int(s) for s in slept]}s, "
          f"resets on success, caps at {agoda.COOLDOWN_MAX}s")


def _selftest_booking_fill(cats):
    """The gap-fill's CONTRACT, with the network stubbed out.

    What must hold, and what each check is defending against:
      * it fills only rooms Agoda left empty          (never overwrites a match)
      * it never adds a room Bookme does not sell     (a private listing's own
                                                       room set is not the
                                                       hotel's -- see
                                                       resolve_verified)
      * a hotel it cannot geo-verify changes NOTHING  (unearned imagery)
      * every zero comes back with a distinguishable  (four different causes
        reason                                         must not read alike)
    """
    hotel = {"name": "Test Grand Hotel", "lat": 25.0, "lon": 55.0}
    real_resolve, real_union = booking.resolve_verified, booking.rooms_union
    try:
        # A stub that ASSERTS ITS OWN CONTRACT: every real call site (both the
        # base shapes and each escalation shape) must pass `rooms_union`
        # (check_in, check_out) DATE pairs, never the raw (weeks, nights)
        # tuples `config.BOOKING_PROBES_ESCALATION` stores them as. A prior
        # version of the escalation loop passed the raw tuples straight
        # through, skipping `booking_shapes()`'s conversion -- silent to every
        # other assertion here (they only check the OUTCOME), and it would
        # have crashed on the first live call as `int has no attribute
        # isoformat`. Caught only by adding this shape check to the stub
        # itself, which a plain `lambda *_a, **_k: ...` cannot do.
        def checked_union(_s, _slug, _country, shapes, **_kw):
            for ci_, co_ in shapes:
                assert hasattr(ci_, "isoformat") and hasattr(co_, "isoformat"), (
                    f"rooms_union got non-date shapes: {shapes!r} -- the "
                    f"(weeks, nights) -> date conversion was skipped")
            return [
                {"booking_room_id": "1", "room_name": "Deluxe King Room",
                 "images": ["https://b/1.jpg", "https://b/2.jpg"]},
                {"booking_room_id": "2", "room_name": "Penthouse Booking Invented",
                 "images": ["https://b/9.jpg"]}]

        booking.resolve_verified = lambda *_a, **_k: {
            "slug": "test-grand", "country": "ae", "km": 0.02, "how": "geo",
            "_html": None}
        booking.rooms_union = checked_union

        rows = [_room("Deluxe King Room", [], cats),
                _room("Superior Twin Room", ["https://agoda/a.jpg"], cats)]
        n, note, names = booking_fill(None, hotel, rows, cats)

        assert n == 1, (n, note)
        by = {r["name"]: r for r in rows}
        assert by["Deluxe King Room"]["source_images"] == [
            "https://b/1.jpg", "https://b/2.jpg"], by
        assert by["Deluxe King Room"]["image_source"] == "booking"
        assert names == ["Deluxe King Room"], names
        # the Agoda-matched room must be untouched, images AND provenance
        assert by["Superior Twin Room"]["source_images"] == ["https://agoda/a.jpg"]
        assert by["Superior Twin Room"]["image_source"] == "agoda"
        # and no Booking-only room may have been appended
        assert len(rows) == 2, f"gap-fill invented a room: {[r['name'] for r in rows]}"

        # a hotel that cannot be verified must leave the rows exactly as found
        booking.resolve_verified = lambda *_a, **_k: None
        rows2 = [_room("Deluxe King Room", [], cats)]
        n2, note2, _ = booking_fill(None, hotel, rows2, cats)
        assert n2 == 0 and rows2[0]["source_images"] == [], (n2, rows2)
        assert "verified" in note2, note2

        # rooms exist but match nothing -> a DIFFERENT reason, not the same zero
        booking.resolve_verified = lambda *_a, **_k: {
            "slug": "test-grand", "country": "ae", "km": 0.02, "how": "geo"}
        booking.rooms_union = lambda *_a, **_k: [
            {"booking_room_id": "7", "room_name": "Nothing Like It",
             "images": ["https://b/7.jpg"]}]
        n3, note3, _ = booking_fill(None, hotel,
                                    [_room("Deluxe King Room", [], cats)], cats)
        assert n3 == 0 and "none matched" in note3, (n3, note3)

        # THE CASE THAT MATTERS: Bookme sells far more rooms than Agoda could
        # show. Every Bookme room Agoda missed is emitted imageless by
        # map_rooms, which makes it a gap, which sends it to Booking -- no
        # separate "agoda shortfall" trigger is needed, and this asserts that
        # rather than leaving it to be re-derived by the next reader.
        booking.resolve_verified = lambda *_a, **_k: {
            "slug": "test-grand", "country": "ae", "km": 0.02, "how": "geo"}
        booking.rooms_union = lambda *_a, **_k: [
            {"booking_room_id": str(i), "room_name": nm, "images": [f"https://b/{i}.jpg"]}
            for i, nm in enumerate(["Deluxe King Room", "Superior Twin Room",
                                    "Family Suite"], 1)]
        # bookme sells 4, agoda matched only 1 -> 3 imageless rows
        agoda_matched = _room("Studio Apartment", ["https://agoda/s.jpg"], cats)
        shortfall = [agoda_matched] + [_room(n, [], cats) for n in
                                       ("Deluxe King Room", "Superior Twin Room",
                                        "Family Suite")]
        nS, _noteS, namesS = booking_fill(None, hotel, shortfall, cats)
        assert nS == 3, (nS, _noteS)
        assert len(shortfall) == 4, "booking added a room it was not allowed to add"
        assert shortfall[0]["source_images"] == ["https://agoda/s.jpg"], \
            "agoda's own match was clobbered by the second source"
        assert set(namesS) == {"Deluxe King Room", "Superior Twin Room",
                               "Family Suite"}, namesS

        # no gaps at all -> the second source is never even asked
        asked = []
        booking.resolve_verified = lambda *_a, **_k: asked.append(1)
        n4, note4, _ = booking_fill(None, hotel,
                                    [_room("X", ["https://agoda/x.jpg"], cats)], cats)
        assert n4 == 0 and note4 == "no gaps" and not asked, (note4, asked)

        # A room with no candidate images this run, but a picture ALREADY in the
        # database, is not a gap -- fetching for it would buy pages that
        # _split_for_mirroring is about to discard. The key derivation must
        # match that function's, so this asserts on a name needing the same
        # strip/truncate/lower treatment.
        imaged = _room("  Already Imaged Room  ", [], cats)
        key = imaged["name"].strip()[:config.ROOM_NAME_MAX].lower()
        n5, note5, _ = booking_fill(None, hotel, [imaged], cats,
                                    existing={key: {"has_image": True}})
        assert n5 == 0 and note5 == "no gaps" and not asked, (note5, asked)
        # ...but the same room with NO database picture must still be asked
        n6, _note6, _ = booking_fill(None, hotel, [imaged], cats,
                                     existing={key: {"has_image": False}})
        assert asked, "a genuinely imageless room was not offered to the second source"

        # THE REGRESSION TEST for a real bug: the base shapes satisfy nothing,
        # forcing the ESCALATION loop to run, and every call it makes --
        # base AND escalation alike -- must receive real dates. A prior version
        # passed `config.BOOKING_PROBES_ESCALATION`'s raw (weeks, nights)
        # tuples straight through without converting them, invisible to every
        # assertion above because none of them force escalation while also
        # checking what shape `rooms_union` actually received.
        calls = []

        def escalating_union(_s, _slug, _country, shapes, **_kw):
            for ci_, co_ in shapes:
                assert hasattr(ci_, "isoformat") and hasattr(co_, "isoformat"), (
                    f"escalation call #{len(calls) + 1} got non-date shapes: "
                    f"{shapes!r}")
            calls.append(len(shapes))
            # the room only appears from the 3rd call onward, forcing the loop
            # through two flat escalation attempts before it succeeds
            if len(calls) < 3:
                return []
            return [{"booking_room_id": "9", "room_name": "Deluxe King Room",
                     "images": ["https://b/9.jpg"]}]

        booking.resolve_verified = lambda *_a, **_k: {
            "slug": "test-grand", "country": "ae", "km": 0.02, "how": "geo",
            "_html": None}
        booking.rooms_union = escalating_union
        rows7 = [_room("Deluxe King Room", [], cats)]
        n7, note7, names7 = booking_fill(None, hotel, rows7, cats)
        assert len(calls) >= 3, (
            f"escalation stopped before the shape that would have paid: {calls}")
        assert n7 == 1 and names7 == ["Deluxe King Room"], (n7, note7, names7)
    finally:
        booking.resolve_verified, booking.rooms_union = real_resolve, real_union

    # date shapes must be real, distinct, and honour their night counts
    sh = booking_shapes([(1, 1), (4, 2)])
    assert len(sh) == 2 and (sh[0][1] - sh[0][0]).days == 1
    assert (sh[1][1] - sh[1][0]).days == 2 and sh[1][0] > sh[0][0], sh

    # merge_rooms is what makes multi-date probing sound: union, never duplicate
    merged = booking.merge_rooms(
        [{"booking_room_id": "1", "room_name": "A", "images": ["u1"]}],
        [{"booking_room_id": "1", "room_name": "A", "images": ["u1", "u2"]},
         {"booking_room_id": "2", "room_name": "B", "images": []}])
    assert len(merged) == 2, merged
    assert merged[0]["images"] == ["u1", "u2"], merged


def _selftest_harvest():
    """Probe-ladder, failure-mode and crash-recovery checks. CACHE is already
    redirected at a temp dir by the caller."""
    # A fake client whose answer DEPENDS on the shape, so a harvest that stopped
    # at the first answer, or failed to union, cannot pass.
    class _FakeBookme:
        Unavailable, AuthFailed = bookme.Unavailable, bookme.AuthFailed

        def __init__(self):
            self.calls = []

        def availability(self, s, slug, ci, co, adults=None):
            self.calls.append((slug, ci, adults))
            if slug == "dead":
                raise self.Unavailable(slug)
            if slug == "flaky":         # answers only on the 3rd shape asked
                n = sum(1 for c in self.calls if c[0] == "flaky")
                return [{"room_name": "Late Room", "max_occupancy": 2,
                         "accurate_media": False}] if n >= 3 else []
            # distinct room per occupancy: only a UNION sees both
            return [{"room_name": f"Room {adults}ad", "max_occupancy": adults,
                     "accurate_media": False}]

    fake = _FakeBookme()
    real_bookme, globals()["bookme"] = bookme, fake
    try:
        hotels = [{"id": 1, "slug": "good", "name": "Good"},
                  {"id": 2, "slug": "dead", "name": "Dead"},
                  {"id": 3, "slug": "flaky", "name": "Flaky"},
                  {"id": 4, "slug": "", "name": "No Slug"}]
        acc, plog = {}, []
        resolved, unavail = harvest_rooms(None, hotels, acc, plog)
    finally:
        globals()["bookme"] = real_bookme

    assert {r["room_name"] for r in acc[1].values()} == {"Room 2ad", "Room 1ad"}, (
        f"probes were not unioned across occupancies: {acc.get(1)}")
    assert 2 in unavail and not acc.get(2), "a permanent 500 was not recorded"
    assert not any(c[0] == "dead" for c in fake.calls[len(hotels):]), \
        "a permanently-unavailable hotel was re-probed on later shapes"
    assert acc.get(3), "a hotel that only answers on a later shape was given up on"
    assert 4 not in acc, "a hotel with no slug was probed anyway"
    assert 1 in resolved and 3 in resolved
    assert plog and all(p["shape"] and p["check_in"] for p in plog), plog

    # A hotel that answers nothing everywhere must still have been ESCALATED --
    # "0 is never accepted as-is" is the rule this asserts.
    fake2 = _FakeBookme()
    fake2.availability = lambda s, slug, ci, co, adults=None: []
    real_bookme, globals()["bookme"] = bookme, fake2
    try:
        acc2, plog2 = {}, []
        harvest_rooms(None, [{"id": 9, "slug": "silent", "name": "Silent"}],
                      acc2, plog2)
    finally:
        globals()["bookme"] = real_bookme
    phases = {p["phase"] for p in plog2}
    assert "escalation" in phases, (
        f"a hotel holding zero rooms was never escalated: {phases}")
    assert len(plog2) == len(probe_shapes(config.ROOM_PROBES)) + \
        len(probe_shapes(config.ROOM_PROBES_ESCALATION)), \
        "the full ladder did not run for a hotel that answered nothing"

    # A HOTEL WE COULD NOT ASK IS NOT A HOTEL WITH NO ROOMS. If the network
    # fails on every shape, the hotel must NOT come back "resolved" -- otherwise
    # the run publishes it as verified-empty and the ledger records a permanent
    # zero earned by a transient outage. This is the same unearned-zero failure
    # the whole pipeline is built to refuse.
    class _Broken(_FakeBookme):
        def availability(self, s, slug, ci, co, adults=None):
            self.calls.append((slug, ci, adults))
            raise requests.ConnectionError("simulated outage")

    broken = _Broken()
    real_bookme, globals()["bookme"] = bookme, broken
    try:
        acc3, plog3 = {}, []
        res3, unavail3 = harvest_rooms(
            None, [{"id": 7, "slug": "unreachable", "name": "Unreachable"}],
            acc3, plog3)
    finally:
        globals()["bookme"] = real_bookme
    assert 7 not in res3, (
        "a hotel that only ever failed to be ASKED was reported as resolved -- "
        "downstream that is indistinguishable from a verified empty hotel")
    assert 7 not in unavail3, "a transport failure was recorded as a permanent 500"
    assert not acc3.get(7), acc3
    assert all(p["errors"] and not p["answered"] for p in plog3), (
        f"transport failures were not logged as errors: {plog3[:2]}")
    # and it must still have exhausted the full ladder before giving up
    assert len(plog3) == len(probe_shapes(config.ROOM_PROBES)) + \
        len(probe_shapes(config.ROOM_PROBES_ESCALATION)), \
        "an unreachable hotel was abandoned before every shape was tried"

    # A run interrupted mid-probe keeps everything already earned rather than
    # discarding the pass -- live_rooms is mutated in place, never rebuilt.
    global _STOP
    stop_was = _STOP
    partial = _FakeBookme()
    real_bookme, globals()["bookme"] = bookme, partial
    try:
        acc4, plog4 = {}, []
        _STOP = True
        harvest_rooms(None, [{"id": 5, "slug": "good", "name": "Good"}],
                      acc4, plog4)
    finally:
        globals()["bookme"] = real_bookme
        _STOP = stop_was
    assert plog4 == [] and acc4 == {}, "a stop request did not halt probing"

    # -- crash recovery: the probe checkpoint ------------------------------
    # A machine that reboots part-way through a city harvest must resume, not
    # re-probe from zero -- and must NEVER resume onto a different hotel set.
    if True:
        hotels_a = [{"id": 1, "slug": "good", "name": "Good"},
                    {"id": 2, "slug": "dead", "name": "Dead"}]
        scope_a = _probe_scope(hotels_a)
        rooms_a = {1: {"R1": {"room_name": "R1", "max_occupancy": 2,
                              "accurate_media": False}}}
        _save_probe_state(scope_a, rooms_a, {1}, {2}, {"base|+1w wknd 2ad 1n|2026-01-03"})

        back = {}
        res, unav, shapes_done = _load_probe_state(scope_a, back)
        assert back[1]["R1"]["room_name"] == "R1", back
        assert res == {1} and unav == {2} and len(shapes_done) == 1

        # a DIFFERENT hotel set must not inherit "these shapes are done" --
        # otherwise those hotels are reported probed-and-empty having never
        # been asked, the unearned zero arriving via the cache
        other = {}
        r2, u2, s2 = _load_probe_state(
            _probe_scope([{"id": 99, "slug": "x", "name": "X"}]), other)
        assert (r2, u2, s2, other) == (set(), set(), set(), {}), \
            "a checkpoint leaked across different hotel sets"

        # re-tuning the ladder must also invalidate it, or new shapes are
        # skipped as already-done
        probes_was = config.ROOM_PROBES
        try:
            config.ROOM_PROBES = probes_was + [(9, 2, 1)]
            assert _probe_scope(hotels_a) != scope_a, \
                "changing the probe ladder did not invalidate the checkpoint"
        finally:
            config.ROOM_PROBES = probes_was

        # a corrupt checkpoint is discarded, never raised
        with open(PROBE_CACHE, "w", encoding="utf-8") as f:
            f.write("{not json")
        broke = {}
        assert _load_probe_state(scope_a, broke) == (set(), set(), set())
        assert broke == {}

        # and a resumed harvest skips the shapes already recorded
        _save_probe_state(scope_a, rooms_a, {1}, set(),
                          {f"base|{s[3]}|{s[0].isoformat()}"
                           for s in probe_shapes(config.ROOM_PROBES)})
        resumed = _FakeBookme()
        real_bookme, globals()["bookme"] = bookme, resumed
        try:
            acc5, plog5 = {}, []
            harvest_rooms(None, [{"id": 1, "slug": "good", "name": "Good"},
                                 {"id": 2, "slug": "dead", "name": "Dead"}],
                          acc5, plog5)
        finally:
            globals()["bookme"] = real_bookme
        assert not any(p["phase"] == "base" for p in plog5), (
            f"a resumed harvest re-ran base shapes it had already completed: "
            f"{[p['shape'] for p in plog5][:3]}")
        assert acc5.get(1), "the resumed harvest lost the check-pointed rooms"


def _selftest_mapping(cats):
    """Room-mapping, review-CSV and image-split checks. Pure, no network."""
    # -- wasted-work fix: an already-imaged room must never reach mirroring --
    to_pub = [
        _room("Already Imaged", ["cand1.jpg", "cand2.jpg"], cats, size_sqft=200),
        _room("Missing Image", ["cand3.jpg"], cats, size_sqft=150),
        _room("Brand New Room", ["cand4.jpg"], cats),
    ]
    existing = {
        "already imaged": {"id": 1, "has_image": True, "has_size": True},
        "missing image": {"id": 2, "has_image": False, "has_size": True},
        # "brand new room" absent entirely -- never seen before
    }
    need_mirror, already_imaged = _split_for_mirroring(to_pub, existing)
    assert {r["name"] for r in need_mirror} == {"Missing Image", "Brand New Room"}, (
        f"a room needing mirroring was skipped, or an already-imaged room "
        f"was not: need_mirror={[r['name'] for r in need_mirror]}")
    assert {r["name"] for r in already_imaged} == {"Already Imaged"}
    skipped = already_imaged[0]
    assert skipped["thumbnail"] is None and skipped["images"] == [], (
        "a skipped room must carry the explicit empty-mirror shape, not "
        "leftover candidate state, or db.publish() misreads it as a real "
        "mirror attempt")
    # size_sqft must survive untouched on the skipped room -- it never needed
    # mirroring to begin with, and db.publish()'s COALESCE backfills it
    # independently of the image path
    assert skipped["size_sqft"] == 200

    # mutation check: if the has_image gate is ever flipped or dropped, this
    # must fail -- proves the test has teeth, not just a passing assertion
    broken_existing = {k: {**v, "has_image": False} for k, v in existing.items()}
    need_mirror2, already_imaged2 = _split_for_mirroring(
        [_room("Already Imaged", ["x"], cats)], broken_existing)
    assert need_mirror2 and not already_imaged2, (
        "the has_image check itself is not gating anything -- the mutation "
        "that should break this test did not")

    bm = [{"room_name": "Deluxe Canal View"},      # confident match
          {"room_name": "Executive Suite"},        # vetoed against Executive Room
          {"room_name": "Zaabeel Room King"}]      # nothing like it on agoda
    ag = [{"agoda_room_id": 1, "room_name": "Deluxe Canal View",
           "images": ["a", "b", "c"], "size_sqft": 334},
          {"agoda_room_id": 2, "room_name": "Executive Room", "images": ["d"],
           "size_sqft": 400},
          {"agoda_room_id": 3, "room_name": "Presidential Suite", "images": ["e"],
           "size_sqft": None}]      # Agoda itself sometimes has no size either
    pub, review, unmatched = map_rooms(bm, ag, cats)
    by_name = {r["name"]: r for r in pub}
    assert by_name["Deluxe Canal View"]["source_images"] == ["a", "b", "c"]
    assert by_name["Deluxe Canal View"]["category"] == "Deluxe Room"
    assert by_name["Deluxe Canal View"]["size_sqft"] == 334, "size_sqft not threaded through a matched pair"
    # a class veto must not let suite images land on a room, or vice versa
    assert by_name["Executive Suite"]["source_images"] == []
    assert by_name["Executive Suite"]["size_sqft"] is None, \
        "a vetoed pair must not carry the OTHER room's size either"
    assert by_name["Zaabeel Room King"]["source_images"] == []
    assert by_name["Zaabeel Room King"]["category"] == "General"
    # agoda-only rooms are published under agoda's own name, with their images
    # AND their size -- Presidential Suite has none, which must surface as
    # None, not crash or silently coerce to 0.
    assert by_name["Presidential Suite"]["source_images"] == ["e"]
    assert by_name["Presidential Suite"]["size_sqft"] is None
    # a room claimed by a bookme match is not ALSO published under agoda's name
    assert len([r for r in pub if r["name"] == "Deluxe Canal View"]) == 1
    # A veto says "these two are not the same room" -- NOT "the agoda room is
    # fake". Executive Room is a real room of the hotel that Bookme's live feed
    # did not show, so it is published on its own, with its own images AND size.
    assert by_name["Executive Room"]["source_images"] == ["d"]
    assert by_name["Executive Room"]["size_sqft"] == 400
    assert {r["bookme_room_name"] for r in unmatched} >= {"Zaabeel Room King"}

    # REGRESSION: losing the greedy draw for a contested Agoda room is NOT
    # itself evidence against a room -- if a losing room's OWN best candidate
    # already passed every veto, it must be handled on that evidence alone,
    # never buried in unmatched (and never with a veto_reason borrowed from
    # some unrelated candidate). Found live: "One Bedroom Standard Apartment"
    # was reported unmatched, best_agoda_room="One Bedroom Apartment" (a
    # clean, veto-free 100% match, just stolen by the exact-name rival below)
    # sitting next to veto_reason="class apartment != room" -- a reason that
    # actually belonged to "Deluxe Lagoon View Room", compared earlier in the
    # loop. Two stacked bugs, same case: the leaked veto, and contention
    # discarding a confident match outright regardless of its own score.
    bm_contend = [{"room_name": "One Bedroom Apartment"},           # wins the exact tie
                  {"room_name": "One Bedroom Standard Apartment"},  # loses it, own score >= ACCEPT
                  {"room_name": "Modern Apartment Retreat"}]        # loses it, own score in REVIEW band
    ag_contend = [{"agoda_room_id": 1, "room_name": "One Bedroom Apartment", "images": ["x"]},
                  {"agoda_room_id": 2, "room_name": "Deluxe Lagoon View Room", "images": ["y"]}]
    pub_c, review_c, unmatched_c = map_rooms(bm_contend, ag_contend, cats)
    pub_by_name = {r["name"]: r for r in pub_c}
    assert pub_by_name["One Bedroom Standard Apartment"]["source_images"] == ["x"], (
        "a contested match that independently clears ROOM_ACCEPT must still publish "
        "with the SAME candidate's images, not fall to unmatched")
    review_names = {r["bookme_room_name"] for r in review_c}
    assert "Modern Apartment Retreat" in review_names, (
        "a contested match scoring in the review band must land in review, not unmatched")
    contested = {"One Bedroom Standard Apartment", "Modern Apartment Retreat"}
    assert not (contested & {r["bookme_room_name"] for r in unmatched_c}), (
        f"contested-but-plausible rooms leaked into unmatched: {unmatched_c}")

    # RATE-PLAN VARIANTS ALL GET THE IMAGERY. Bookme sells one physical room
    # under many names (refundable/non-refundable, package rate, board basis,
    # a bracketed bedbank echo, an en-dash instead of a comma). Every one of
    # those is a real row on Bookme's side sitting on the same wrong hotel-level
    # photo, so every one must receive the corrected gallery -- consolidating
    # them to a single winner would fix one row and silently strand the rest.
    #
    # They are grouped only for MATCHING (match.norm_room strips the rate-plan
    # cruft, so they all score against the same Agoda room); the greedy
    # assignment then hands the Agoda room to one of them, and the rest are
    # rescued by their OWN score via best_seen. This asserts the whole chain --
    # if greedy contention ever starts burying the losers again, this fails.
    variants = [{"room_name": n} for n in (
        "Deluxe Room", "Deluxe Room Non Refundable", "Deluxe Room (Package Rate)",
        "Deluxe Room – Breakfast Included", "Deluxe Room [Deluxe Room NRHB]")]
    one_ag = [{"agoda_room_id": 1, "room_name": "Deluxe Room",
               "images": ["a", "b"], "size_sqft": 300}]
    vpub, vrev, vun = map_rooms(variants, one_ag, cats)
    assert len(vpub) == len(variants), (
        f"{len(variants)} rate-plan variants collapsed to {len(vpub)} published "
        f"rows -- the others would keep their wrong photo")
    assert all(r["source_images"] == ["a", "b"] for r in vpub), (
        f"a rate-plan variant was published without the imagery: "
        f"{[(r['name'], r['source_images']) for r in vpub]}")
    assert all(r["size_sqft"] == 300 for r in vpub), "a variant lost size_sqft"
    assert not vun, f"a rate-plan variant fell to unmatched: {vun}"
    assert {r["name"] for r in vpub} == {v["room_name"] for v in variants}, \
        "variants were not published under their own Bookme names"

    # A review-band pair is CSV-only by default (config.REVIEW_BAND_CREATES_ROOM
    # = False, the original decision) -- no v2_rooms row either way, and NEVER
    # published as an agoda-only room either, which would put two near-identical
    # rooms live under different names for what is probably the same room.
    # An accessibility mismatch is deliberately HELD to the review band rather
    # than vetoed, so a human still sees the candidate image and can recover
    # the match -- a buried pair would just leave the room on its old wrong
    # hotel photo. (Bed-disjoint pairs like king vs double/queen are vetoed
    # outright instead; see match.py.)
    pub2, review2, _ = map_rooms(
        [{"room_name": "Cosy Room"}],
        [{"agoda_room_id": 9, "room_name": "Cosy Accessible Room",
          "images": ["q"]}], cats)
    assert len(review2) == 1, f"expected a review-band pair, got {review2}"
    assert review2[0]["score"] < config.ROOM_ACCEPT, \
        "accessibility mismatch must never auto-publish"
    assert pub2 == [], f"review band created a row despite the CSV-only default: {pub2}"
    assert all(r["category_id"] for r in pub), "a room went out with no category"

    # NULL category_id must be structurally impossible, not merely rare. Found
    # live: "Deluxe Room" -- a name that classifies to a category that
    # genuinely exists -- still landed with room_category_id NULL, because
    # cat_ids was incomplete at write time and _room() trusted the lookup
    # blindly. Two guarantees, tested independently:
    #
    # 1. _room() itself: even handed a cat_ids MISSING the room's own category,
    #    it must fall back to General rather than emit None.
    broken_cats = {n: i for i, n in enumerate(categories.ALL, 1) if n != "Deluxe Room"}
    assert "Deluxe Room" not in broken_cats, "test setup: Deluxe Room must be absent"
    fallback_room = _room("Deluxe Room With Balcony", ["x"], broken_cats)
    assert fallback_room["category"] == "Deluxe Room", fallback_room
    assert fallback_room["category_id"] == broken_cats.get(categories.FALLBACK), (
        f"a room whose real category was missing from cat_ids did not fall "
        f"back to General: {fallback_room}")
    assert fallback_room["category_id"] is not None, (
        "NULL category_id reached a room dict -- this must be impossible")

    # 2. _validated_cat_ids() refuses to let an incomplete mapping reach ANY
    #    hotel in the first place -- the actual root-cause fix, not just the
    #    per-room symptom patch above.
    real_sync, db.sync_categories = db.sync_categories, lambda conn: (broken_cats, [])
    try:
        try:
            _validated_cat_ids(object())
            raised = False
        except SystemExit:
            raised = True
    finally:
        db.sync_categories = real_sync
    assert raised, (
        "_validated_cat_ids() let an incomplete category mapping through -- "
        "a run would have proceeded to write NULL categories across every "
        "hotel it processed")

    # apply_review_decisions: a real map_rooms() review row, round-tripped
    # through the exact CSV writer/reader the pipeline uses, must reconstruct
    # the same candidate gallery and size -- and the decision filter must
    # only ever pick up an explicit approval, never a blank or a rejection.
    approve_row = {**review2[0], "hotel_id": "1", "hotel_name": "Cosy Hotel",
                  "candidate_images": "https://x/1.jpg|https://x/2.jpg",
                  "candidate_size_sqft": "280", "decision": "Approve"}
    pending_row = {**approve_row, "decision": ""}
    rejected_row = {**approve_row, "decision": "no"}
    approved = _approved([approve_row, pending_row, rejected_row])
    assert approved == [approve_row], \
        f"decision filter picked up a non-approval: {approved}"
    rebuilt = _room_from_review_row(approve_row, cats)
    assert rebuilt["source_images"] == ["https://x/1.jpg", "https://x/2.jpg"]
    assert rebuilt["size_sqft"] == 280
    assert rebuilt["name"] == approve_row["bookme_room_name"]

    # the pipe-join survives an actual disk round-trip through _write_csv,
    # not just direct dict construction -- proves the CSV format itself
    # (quoting, the empty-size case) is what apply_review_decisions can read
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".csv", mode="w", delete=False) as tf:
        tmp_path = tf.name
    try:
        _write_csv(tmp_path, REVIEW_COLUMNS, [approve_row, pending_row])
        with open(tmp_path, newline="", encoding="utf-8") as f:
            read_back = list(csv.DictReader(f))
        assert len(_approved(read_back)) == 1, read_back
        reloaded = _room_from_review_row(_approved(read_back)[0], cats)
        assert reloaded["source_images"] == ["https://x/1.jpg", "https://x/2.jpg"]
        assert reloaded["size_sqft"] == 280
    finally:
        os.remove(tmp_path)
    # a candidate with no size at all must come back as None, not "" or 0
    no_size = _room_from_review_row({**approve_row, "candidate_size_sqft": ""}, cats)
    assert no_size["size_sqft"] is None, no_size["size_sqft"]

    # TIE-BREAKING. token_set_ratio rates a subset as a perfect 100, so
    # "Junior Suite" scores 100 against BOTH "Junior Suite Deluxe" and its own
    # exact twin. The exact twin must win regardless of which Agoda lists
    # first, or a room gets a pricier room's photographs -- found live, where
    # it also produced a phantom duplicate row.
    for ag_order in ([{"agoda_room_id": 1, "room_name": "Junior Suite Deluxe",
                       "images": ["deluxe"]},
                      {"agoda_room_id": 2, "room_name": "Junior Suite",
                       "images": ["exact"]}],
                     [{"agoda_room_id": 2, "room_name": "Junior Suite",
                       "images": ["exact"]},
                      {"agoda_room_id": 1, "room_name": "Junior Suite Deluxe",
                       "images": ["deluxe"]}]):
        tie_pub, _, _ = map_rooms([{"room_name": "Junior Suite"}], ag_order, cats)
        by = {r["name"]: r for r in tie_pub}
        assert by["Junior Suite"]["source_images"] == ["exact"], (
            f"tie broken by list order, not by exactness: "
            f"{by['Junior Suite']['source_images']}")
        # the loser is still published under its OWN name, not as a duplicate
        assert "Junior Suite Deluxe" in by, sorted(by)
        names = [r["name"] for r in tie_pub]
        assert len(names) == len(set(names)), f"duplicate room names: {names}"

    # PROPERTY, not example: the mapping must be INVARIANT under the order of
    # either platform's room list. A pass that only checks the two orderings I
    # happened to hit proves nothing about the ones I did not; this shuffles a
    # contested set many times and demands byte-identical output every time.
    # If any decision anywhere in map_rooms consults array position, this fails.
    import random as _random
    bm_set = [{"room_name": n} for n in
              ("Deluxe Room", "Deluxe Room Sea View", "Junior Suite",
               "Standard Twin", "Executive Suite")]
    ag_set = [{"agoda_room_id": i, "room_name": n, "images": [f"img{i}"]}
              for i, n in enumerate(
                  ("Junior Suite", "Deluxe Room", "Deluxe Room Sea View",
                   "Junior Suite Deluxe", "Standard Twin Room",
                   "Presidential Suite"), start=1)]

    def _fingerprint(p, r, u):
        return (sorted((x["name"], tuple(x["source_images"])) for x in p),
                sorted((x["bookme_room_name"], x["agoda_room_name"]) for x in r),
                sorted(x["bookme_room_name"] for x in u))

    baseline = _fingerprint(*map_rooms(bm_set, ag_set, cats))
    rng = _random.Random(0)
    for _ in range(60):
        b2, a2 = list(bm_set), list(ag_set)
        rng.shuffle(b2)
        rng.shuffle(a2)
        assert _fingerprint(*map_rooms(b2, a2, cats)) == baseline, (
            "map_rooms output changed when the input lists were reordered -- "
            "some decision still depends on array position, not evidence")
    # and no room may ever be emitted twice under one name
    pub_names = [x["name"] for x in map_rooms(bm_set, ag_set, cats)[0]]
    assert len(pub_names) == len(set(pub_names)), pub_names

    # Address handling: REMOVAL, so field order can never cost us the city.
    # These are the formats the catalogue actually contains, including the ones
    # that returned nothing at all under the previous truncating version.
    assert _address_query("Sheikh Zayed Road, Dubai 12345, United Arab Emirates",
                          "United Arab Emirates") == "Sheikh Zayed Road Dubai"
    assert _address_query("Sheikh Zayed Road, Dubai, U.A.E.",
                          "United Arab Emirates") == "Sheikh Zayed Road Dubai"
    # city AFTER the country and postcode -- truncation lost it entirely
    assert _address_query("vardanants 15/4, 0010, armenia, yerevan",
                          "Armenia") == "vardanants 15 4 yerevan"
    # these two returned None before: an entire address style with no fallback
    assert _address_query("Plot 4502, Gulshan, Dhaka 1212, Bangladesh",
                          "Bangladesh") == "Plot Gulshan Dhaka"
    # a city that EMBEDS its country name must keep it -- Panama City, Mexico
    # City, Kuwait City. Blanket removal left a bare "City".
    assert _address_query("Calle 50 1234, Panama City, Panama",
                          "Panama") == "Calle 50 Panama City"
    assert "Mexico City" in _address_query(
        "Av. Reforma 100, Mexico City, Mexico", "Mexico")
    # a city that IS its country keeps the token entirely -- ~10% of this
    # catalogue, because whole countries are stored as single cities
    assert _address_query("Copacabana 55, Rio, Brazil", "Brazil", "brazil") \
        == "Copacabana 55 Rio Brazil"
    assert _address_query("Orchard Road 238859, Singapore",
                          "Singapore", "Singapore") == "Orchard Road Singapore"
    # the city token must survive, which is the whole point of the query
    assert "Sao Paulo" in _address_query(
        "Avenida Paulista 1000, Sao Paulo, Brazil", "Brazil")
    assert "Paris" in _address_query(
        "5 Rue de Rivoli 75001, Paris, France", "France")
    # a country misspelled in the address must not destroy the locality tokens
    assert "sarajevo" in _address_query(
        "obala kulina bana 41, , sarajevo, 71000, bosnia and herzegowina",
        "Bosnia and Herzegovina")
    # junk initials must never be built, or they delete real tokens
    assert _NAME_CONNECTORS & {"and", "the"}, "connector list went missing"
    assert "Bahnhofstrasse" in _address_query(
        "Bahnhofstrasse 12, Sarajevo, Bosnia and Herzegovina",
        "Bosnia and Herzegovina")
    assert _address_query("Some Street 4001, Ballito", None) == "Some Street Ballito"
    assert _address_query("", "Peru") is None
    assert _address_query("tiny", "Peru") is None

    # image cap
    r1 = _room("Deluxe Room", [f"i{i}" for i in range(50)], cats)
    assert len(r1["source_images"]) == config.MAX_IMAGES_PER_ROOM

    # thumbnail/attachment split, preserved PER ROOM under concurrent fetch --
    # this is the part that would break silently if results got reassociated
    # by arrival order instead of by position.
    r1["source_images"] = ["x", "y", "z"]
    r2 = _room("Superior Room", ["a", "b"], cats)
    real_mirror, cos.mirror = cos.mirror, lambda u, session=None: f"cos:{u}"
    try:
        n = mirror_all_images([r1, r2], None)
    finally:
        cos.mirror = real_mirror
    assert n == 5
    assert r1["thumbnail"] == "cos:x" and r1["images"] == ["cos:y", "cos:z"], r1
    assert r2["thumbnail"] == "cos:a" and r2["images"] == ["cos:b"], r2

    # concurrency is real, not just structurally harmless: 6 "slow" fetches
    # sequentially would be >= 0.3s; the whole point of mirror_all_images is
    # that they overlap. A generous ceiling well under that proves it without
    # being a flaky exact-timing assertion.
    import time as _time
    slow = lambda u, session=None: (_time.sleep(0.05), f"cos:{u}")[1]
    r3 = _room("Timing Room", [f"p{i}" for i in range(6)], cats)
    real_mirror, cos.mirror = cos.mirror, slow
    try:
        t0 = _time.monotonic()
        mirror_all_images([r3], None)
        elapsed = _time.monotonic() - t0
    finally:
        cos.mirror = real_mirror
    assert elapsed < 0.2, f"6 fetches took {elapsed:.2f}s -- not running concurrently"

    print(f"OK: probe ladder (union + zero-never-accepted), room mapping, "
          f"{len(review)} review row(s), image split, {elapsed * 1000:.0f}ms for "
          f"6 concurrent images (sequential would be >=300ms)")


# --------------------------------------------------------------- the wizard
class _Back(Exception):
    """Raised by _ask() when the operator types 'b' -- caught by _wizard() to
    step back exactly one question."""


def _parse_slug_list(inline, path):
    """--slugs and --slugs-file, unioned, order-preserving, de-duplicated.

    Blank lines and '#' comments are ignored in the file so an operator can
    keep a curated, annotated list around rather than a bare one-per-line
    dump. Order is preserved (not sorted) because the log's per-hotel
    progress lines are easier to follow against a list the operator
    recognises the order of.
    """
    out, seen = [], set()

    def add(s):
        s = s.strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)

    for s in (inline or "").split(","):
        add(s)
    if path:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0]
                add(line)
    return out


def _ask(prompt, back=True):
    hint = " ['b' back, 'q' quit]" if back else " ['q' quit]"
    raw = input(f"{prompt}{hint}: ").strip()
    if raw.lower() in ("q", "quit", "exit"):
        raise SystemExit("aborted")
    if back and raw.lower() in ("b", "back"):
        raise _Back
    return raw


def _menu(title, options, back=True):
    """Print a numbered menu, return the CHOSEN OPTION'S VALUE.

    `options` is [(label, value), ...]. The operator types the number they
    read on screen, never a keyword ('all', 'yes') and never a raw id they'd
    have to copy off an earlier line -- typing is reserved for the one place
    in this wizard nothing on screen could stand in for it (an operator-
    chosen count or range). 1-indexed because that is what a person reads off
    a numbered list, not 0-indexed developer habit.
    """
    print(f"\n{title}")
    for i, (label, _val) in enumerate(options, 1):
        print(f"  {i}) {label}")
    hint = " ['b' back, 'q' quit]" if back else " ['q' quit]"
    while True:
        raw = input(f"  > {hint}: ").strip()
        if raw.lower() in ("q", "quit", "exit"):
            raise SystemExit("aborted")
        if back and raw.lower() in ("b", "back"):
            raise _Back
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][1]
        print(f"  enter a number from 1 to {len(options)}")


def _ask_yn(prompt, default=True):
    """A plain y/n confirmation -- deliberately NOT a numbered menu. Two
    genuinely opposite options with an obvious default read faster as
    '[Y/n]: ' than as a two-line menu forcing a '1' or '2' keystroke for
    something that fits in one character. Enter alone takes the default.
    """
    hint = "[Y/n]" if default else "[y/N]"
    raw = input(f"{prompt} {hint}: ").strip().lower()
    if raw in ("q", "quit", "exit"):
        raise SystemExit("aborted")
    if not raw:
        return default
    return raw in ("y", "yes")


def _city_runnable(conn, led, city_id, fresh_ids=None):
    """(total, fresh, runnable) hotel counts for one city_id, cross-referenced
    against the published ledger -- `fresh` is published within
    config.LEDGER_STALE_DAYS and will be SKIPPED, not run again.

    Fetches ids ONLY. This is a counting question asked while the operator is
    sat waiting at a prompt, and it used to pull every hotel's full row --
    name, address, coordinates -- for a city that can hold 6,732 of them, to
    then use nothing but the id. `fresh_ids` is passed in so the ledger is not
    re-scanned once per matching city either.
    A hotel published but still owing a picture (ledger reason
    "needs_image_backfill") counts as RUNNABLE here, matching main()'s actual
    selection -- otherwise this preview and the real run would disagree about
    the same city.
    """
    fresh_ids = led.fresh_ids() if fresh_ids is None else fresh_ids
    ids = db.hotel_ids(conn, [city_id])
    skip_ids = fresh_ids - led.unresolved_ids()
    n_fresh = sum(1 for i in ids if i in skip_ids)
    return len(ids), n_fresh, len(ids) - n_fresh


_RANGE_RE = re.compile(r"^\s*(\d+)\s*[-:]\s*(\d+)\s*$")


_BOUND_INVALID = object()          # sentinel: distinct from a valid (None, 0) == "all"


def _parse_bound_reply(reply):
    """'all' / a positive count / an 'A-B' or 'A:B' range -> (bound, offset).
    Returns _BOUND_INVALID for anything unrecognised, so the wizard step can
    re-ask with the same message rather than duplicating the parsing rules at
    the call site -- `bound is _BOUND_INVALID` is the only check a caller
    needs, never a bare `is None` (that's the valid "all" case).

    The range is 1-INDEXED and INCLUSIVE of both ends, matching how a person
    reads it aloud: '6-7' is the 6th and 7th hotel (2 of them), '1000-1643' is
    the 1000th through the 1643rd (644 of them) -- 'A-A' (equal ends) is a
    valid single-hotel selection, only A > B is rejected as backwards. This is
    the ONE conversion point from that human-facing convention to the
    (start, count) pair the rest of the pipeline slices with -- `offset` ends
    up 0-indexed here so `todo[offset:offset+bound]` lands on the right
    elements, but the CALLER-FACING number ('A' as typed) is always 1-indexed.

    The position is WITHIN this city's own list (the same order --limit alone
    already walks, v2_common_hotels.id ascending), never a raw database id --
    ids are global across every city, so a literal id range would not even
    stay inside the chosen one.
    """
    reply = reply.strip()
    if reply.lower() == "all":
        return None, 0
    if reply.isdigit() and int(reply) > 0:
        return int(reply), 0
    m = _RANGE_RE.match(reply)
    if m:
        start, end = int(m.group(1)), int(m.group(2))
        if start >= 1 and end >= start:
            return end - start + 1, start - 1
    return _BOUND_INVALID


def _wizard(conn, led):
    """Interactive city + scope picker, for when the operator did not pass
    --city. Every question accepts 'b' to step back one question and 'q' to
    quit outright. Returns
    (chosen_cities, city_query, limit_or_None, offset, rooms_from, skip_gate).

    `skip_gate` answers the VERY FIRST question, before city selection even
    starts: does this operator want to be asked again, after discovery, before
    downloading and publishing begin? Asked up front rather than only
    discovered at the end of a (possibly long) discovery phase, so someone who
    already knows they want a hands-off run can say so once and walk away,
    instead of sitting through discovery only to find a second prompt waiting
    for them. 'n' here sets the SAME `--yes` semantics the gate already
    understands from the CLI -- no separate flag or code path, just an
    interactive way to pre-answer it.
    """
    run_hands_off = not _ask_yn(
        "\nPause and let you review the discovered plan before downloading "
        "images and publishing starts?", default=True)
    step, city_query, matches, chosen, stats = 0, None, None, None, None
    bound, offset = None, 0
    while True:
        try:
            if step == 0:
                city_query = _ask("\nEnter a city name or numeric city_id", back=False)
                if not city_query:
                    continue
                matches = db.resolve_city(conn, city_query)
                if not matches:
                    print(f"  no city matches {city_query!r}; try again")
                    continue
                step = 1
                continue

            if step == 1:
                print(f"\ncities matching {city_query!r}:")
                _fresh = led.fresh_ids()      # read once, not once per city
                stats = {c["id"]: _city_runnable(conn, led, c["id"], _fresh)
                         for c in matches}
                name_w = max(4, max(len((c["name_en"] or "").title()) for c in matches))
                print(f"\n  {'id':>7}  {'name':<{name_w}}  {'total':>6}  "
                     f"{'runnable':>8}  {'fresh*':>6}")
                print(f"  {'-' * 7}  {'-' * name_w}  {'-' * 6}  {'-' * 8}  {'-' * 6}")
                for c in matches:
                    total, fresh, runnable = stats[c["id"]]
                    print(f"  {c['id']:>7}  {(c['name_en'] or '').title():<{name_w}}  "
                         f"{total:>6}  {runnable:>8}  {fresh:>6}")
                print(f"\n  * fresh = published within {config.LEDGER_STALE_DAYS}d, "
                     f"would be skipped")
                reply = _ask("\nrun against which id(s)? [comma-separated, or "
                             "'all']")
                if reply.lower() == "all":
                    chosen = [c for c in matches if c["hotels"]]
                else:
                    wanted = {int(x) for x in re.findall(r"\d+", reply)}
                    chosen = [c for c in matches if c["id"] in wanted]
                if not chosen:
                    print("  that didn't match any of the listed ids; try again")
                    continue
                step = 2
                continue

            if step == 2:
                total = sum(stats[c["id"]][0] for c in chosen)
                fresh = sum(stats[c["id"]][1] for c in chosen)
                runnable = total - fresh
                names = ", ".join((c["name_en"] or "?").title() for c in chosen)
                print(f"\nscope: {names} ({[c['id'] for c in chosen]}) -> "
                     f"{total} hotels total, {fresh} already published "
                     f"(skipped), {runnable} runnable now")
                if runnable == 0:
                    print("  nothing runnable -- every hotel in scope was "
                          "published within the staleness window")
                mode = _menu("how many hotels?", [
                    (f"run all {runnable} runnable hotels", "all"),
                    ("bound to the first N", "bound"),
                    ("a specific range, e.g. the 1000th-1564th", "range"),
                ])
                if mode == "all":
                    bound, offset = None, 0
                elif mode == "bound":
                    reply = _ask("how many hotels?")
                    if not (reply.isdigit() and int(reply) > 0):
                        print("  enter a positive number")
                        continue
                    bound, offset = int(reply), 0
                else:
                    reply = _ask("range (both ends included), e.g. 1000-1564 "
                                 "= the 1000th through the 1564th hotel")
                    bound, offset = _parse_bound_reply(reply)
                    if bound is _BOUND_INVALID:
                        print("  enter a range like '1000-1564'")
                        continue
                if offset and offset >= runnable:
                    print(f"  {offset} is past the {runnable} runnable hotels "
                          f"in scope -- nothing would be selected, try again")
                    continue
                # NOT a question. Bookme's own supplier room names are always
                # pulled -- this pipeline's whole premise is to cover
                # everything reachable, not the cheaper of two options, and a
                # bounded run is a smaller SAMPLE, never a smaller PIPELINE.
                # `--rooms-from agoda` still exists as an explicit CLI flag
                # for the one legitimate reason to skip it (no Bookme partner
                # credentials configured yet) -- see main()'s AuthFailed
                # handling, which degrades to it automatically and loudly if
                # that happens mid-run, never by silently defaulting to it.
                return chosen, city_query, bound, offset, "both", run_hands_off
        except _Back:
            step = max(0, step - 1)


def _row(label, value, width=28, note=""):
    print(f"  {label:<{width}} {value}" + (f"   {note}" if note else ""))


def _print_human_summary(counts, hotels_attempted, destination, rooms_from):
    """Scannable in 5 seconds, cold, a year from now -- key: value rows,
    `x/y (z%)` ratios, tree branches for a breakdown, `[ok]`/`[MISMATCH]`
    instead of a sentence confirming arithmetic. No paragraphs.

    WHY THIS EXISTS: the previous terminal output was `for k, v in
    counts.items(): print(k, v)` -- ~18 internally-named counters with no
    stated relationships, several of which OVERLAP without saying so:
    `hotels_no_agoda_match` counts a hotel regardless of whether it went on
    to publish anyway via Bookme+Booking (D-37's "takeover"), so summing the
    buckets against the hotel total overshot it with no way to tell why.
    `rooms_unmatched` and `rooms_without_candidate_images` read like the same
    number and are not -- the first is "Agoda's own matching failed on this
    room" (kept on record even if Booking later rescues it), the second is
    "still no photo after EVERY source tried".

    Every number below is DERIVED here, not hand-copied from `counts`, so a
    real accounting bug shows up as `[MISMATCH]` on the totals line rather
    than as two counters that quietly stopped agreeing.
    """
    total = hotels_attempted
    via_agoda = counts["hotels_done"] - counts["hotels_published_without_agoda"]
    agoda_blind = counts["hotels_published_without_agoda"]
    published = via_agoda + agoda_blind
    no_listing = (counts["hotels_no_agoda_match"] + counts["hotels_agoda_unreachable"]
                 - agoda_blind)
    zero_rooms = counts["hotels_no_rooms"]
    errored = counts["hotels_error"]
    hotel_sum = published + no_listing + zero_rooms + errored
    ok = "[ok]" if hotel_sum == total else f"[MISMATCH != {total}]"

    def pct(n, d):
        return f"({100*n/d:.0f}%)" if d else "(-)"

    print(f"\n{destination} · {total} hotels selected")
    print("-" * 46)
    print("HOTELS")
    _row("published", f"{published}/{total} {pct(published, total)}")
    _row("  ├─ agoda identified it", via_agoda)
    _row("  └─ agoda-blind (bookme+booking only)", agoda_blind)
    _row("not published", f"{total - published}/{total}")
    _row("  ├─ no listing on any platform", no_listing)
    _row("  ├─ listed but zero rooms", zero_rooms)
    _row("  └─ error", errored)
    _row("total", f"{hotel_sum}/{total}", note=ok)
    if rooms_from == "both":
        u = counts["hotels_unresolved_on_bookme"]
        _row("(info) bookme had nothing live", f"{u}/{total}",
             note="-- not a failure; agoda can cover alone")

    total_rooms = (counts["rooms_with_candidate_images"]
                  + counts["rooms_without_candidate_images"])
    if total_rooms == 0:
        print("\nROOMS\n  (none published this run)")
        return
    with_photo = counts["rooms_with_candidate_images"]
    without_photo = counts["rooms_without_candidate_images"]
    agoda_sourced = with_photo - counts["rooms_filled_from_booking"]
    room_sum = with_photo + without_photo
    room_ok = "[ok]" if room_sum == total_rooms else f"[MISMATCH != {total_rooms}]"

    print(f"\nROOMS · {total_rooms} listings / {published} published hotels")
    print("  (one listing per Bookme SELLABLE NAME -- one physical room sold on "
          "several rate plans = several listings, one shared photo set)")
    print("-" * 46)
    _row("with photo", f"{with_photo}/{total_rooms} {pct(with_photo, total_rooms)}")
    _row("  ├─ agoda", agoda_sourced)
    if rooms_from == "both":
        _row("  └─ booking.com (fallback)",
             f"{counts['rooms_filled_from_booking']}  "
             f"across {counts['hotels_helped_by_booking']} hotels")
    _row("no photo", f"{without_photo}/{total_rooms} {pct(without_photo, total_rooms)}")
    _row("total", f"{room_sum}/{total_rooms}", note=room_ok)

    if rooms_from == "both":
        um = counts["rooms_unmatched"]
        bk = counts["rooms_filled_from_booking"]
        remain = um - bk
        r_ok = "[ok]" if remain == without_photo else \
            f"[MISMATCH, expected {without_photo}]"
        print(f"\n  agoda-unmatched {um}  →  booking rescued {bk}  +  "
              f"still no photo {remain}   {r_ok}")
    if counts["rooms_review"]:
        _row("in review (not counted above)", counts["rooms_review"],
             note="-- see rooms_review.csv, needs a human decision")


# ----------------------------------------------------------------- the run
def main(argv=None):
    # Declared here, first thing: `_STOP` is both read (the per-hotel loop's
    # stop check) and written (the KeyboardInterrupt handler further down) in
    # this function, and Python requires `global` to be the FIRST textual
    # mention of a name in a function -- a `global _STOP` placed later, next
    # to its own assignment, is a SyntaxError given the earlier bare read,
    # not a runtime bug. Caught the hard way: this broke every invocation of
    # `pipeline.run`, including `--selftest`, because a SyntaxError is a
    # compile-time failure -- Python must parse this whole function before
    # running any branch of it, so the early `--selftest` return could not
    # have skipped past it either.
    global _STOP
    if "--selftest" in (argv if argv is not None else sys.argv[1:]):
        return selftest()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--city", help="city name or numeric city_id, e.g. Dubai. "
                    "Omit entirely to enter the interactive wizard instead")
    ap.add_argument("--city-id", type=int, action="append",
                    help="(needs --city) skip the confirmation prompt and use "
                         "these city ids")
    ap.add_argument("--limit", type=int, help="(needs --city) process at most "
                    "N hotels")
    ap.add_argument("--yes", action="store_true",
                    help="(needs --city) accept every matching city")
    ap.add_argument("--rooms-from", choices=("agoda", "both"), default=None,
                    help="(needs --city) 'both' (default) is the real pipeline: "
                         "Bookme's own supplier room NAMES (probed per hotel by "
                         "slug -- no city-wide search, no ref ids, cost scales "
                         "with hotel count, not city size) plus Agoda's images. "
                         "'agoda' skips the Bookme probe entirely -- rooms carry "
                         "Agoda's naming instead -- for when Bookme partner "
                         "credentials aren't available or you deliberately want "
                         "Agoda naming only")
    ap.add_argument("--dry-run", action="store_true",
                    help="do everything except upload images and write to MySQL")
    ap.add_argument("--no-booking", action="store_true",
                    help="skip the booking.com gap-fill pass. On by default, it "
                         "runs only for rooms Agoda left with no photograph, "
                         "and only for hotels it can geo-verify -- so it can "
                         "add coverage but never replace an Agoda match. Turn "
                         "it off to reproduce Agoda-only behaviour exactly")
    ap.add_argument("--random", action="store_true",
                    help="shuffle the hotel selection before applying the "
                         "limit (a representative demo sample instead of the "
                         "lowest ids)")
    ap.add_argument("--offset", type=int, default=0,
                    help="(needs --city) skip the first N hotels of the "
                         "selection before applying --limit -- e.g. "
                         "--offset 1000 --limit 564 processes the 1000th "
                         "through 1564th hotel of this city's own list "
                         "(ordered by v2_common_hotels.id, the SAME order "
                         "--limit alone already uses). NOT a raw database id "
                         "range -- ids are global across every city, so this "
                         "is a position within the chosen city's list, not "
                         "literal id values")
    ap.add_argument("--slugs", metavar="SLUG1,SLUG2,...",
                    help="run against these exact hotels by slug, looked up "
                         "directly in v2_common_hotels -- no --city, no "
                         "wizard, no ledger-freshness skip (naming a hotel "
                         "explicitly means process it now, regardless of "
                         "when it last ran). Combine with --slugs-file to "
                         "union both lists")
    ap.add_argument("--slugs-file", metavar="PATH",
                    help="same as --slugs, one slug per line ('#' comments "
                         "and blank lines ignored) -- for a list too long to "
                         "type on the command line")
    ap.add_argument("--plan-only", action="store_true",
                    help="discover and map every hotel (Agoda + booking.com, "
                         "same as a real run), print the coverage summary, "
                         "then STOP -- no image is downloaded, nothing is "
                         "written to MySQL. The discovery is check-pointed, so "
                         "re-running the same command later (with or without "
                         "this flag) picks up committing exactly where this "
                         "left off, without re-discovering anything")
    ap.add_argument("--apply-reviews", metavar="CSV",
                    help="apply human decisions from a rooms_review.csv (a "
                         "'decision' cell of yes/approve) and exit -- no "
                         "re-match, no city/Agoda re-run, images already "
                         "captured in the CSV are just re-hosted to COS and "
                         "written the same way any other room is")
    a = ap.parse_args(argv)

    if a.apply_reviews:
        signal.signal(signal.SIGINT, _on_sigint)
        signal.signal(signal.SIGTERM, _on_sigterm)
        lock = None if a.dry_run else acquire_run_lock()
        conn = db.connect()
        cat_ids, _created, conn = _validated_cat_ids(conn)
        applied, failed = apply_review_decisions(
            a.apply_reviews, conn, cat_ids,
            session=requests.Session(), dry_run=a.dry_run)
        conn.close()
        if lock is not None:
            lock.close()               # releases the flock; the OS also does this
                                       # on any exit path, crash included
        return 1 if failed and not applied else 0

    if a.slugs or a.slugs_file:
        # An explicit hotel list is a THIRD mode, alongside the wizard and
        # --city -- it bypasses city scope entirely, so the flags that only
        # mean anything as part of choosing a city are refused rather than
        # silently ignored, same reasoning as the check below.
        conflicting = [f for f, v in (("--city", a.city), ("--city-id", a.city_id),
                                      ("--limit", a.limit), ("--offset", a.offset),
                                      ("--random", a.random)) if v]
        if conflicting:
            ap.error(f"--slugs/--slugs-file name the hotels directly and "
                     f"cannot be combined with {', '.join(conflicting)}")
    else:
        # --city-id/--yes/--rooms-from/--limit/--offset only mean anything as
        # pre-answers to questions the wizard would otherwise ask -- silently
        # ignoring them would be a worse experience than refusing to guess
        # which the operator wanted.
        if a.city is None and (a.city_id or a.yes or a.rooms_from
                               or a.limit is not None or a.offset or a.random):
            ap.error("--city-id/--yes/--rooms-from/--limit/--offset/--random "
                     "need --city; omit --city with none of those to use the "
                     "interactive wizard")
        if a.offset and not a.limit:
            ap.error("--offset needs --limit -- an unbounded run has no "
                     "range to offset into")
        if a.offset and a.random:
            ap.error("--offset and --random cannot be combined -- offsetting "
                     "into a list that gets reshuffled every run is not a "
                     "stable range")

    signal.signal(signal.SIGINT, _on_sigint)
    signal.signal(signal.SIGTERM, _on_sigterm)
    # Held for the whole process. A --dry-run writes nothing, so it does not
    # need to exclude anything and must not block a real run either.
    lock = None if a.dry_run else acquire_run_lock()
    run_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    started = time.time()
    conn = db.connect()
    led = ledger.open_ledger()

    # -- scope ---------------------------------------------------------------
    explicit_hotels = None          # set below only in --slugs/--slugs-file mode
    if a.slugs or a.slugs_file:
        slugs = _parse_slug_list(a.slugs, a.slugs_file)
        if not slugs:
            raise SystemExit("--slugs/--slugs-file resolved to an empty list")
        explicit_hotels, conn = db.with_retry(
            conn, lambda c: db.hotels_by_slug(c, slugs), what="slug lookup")
        found = {h["slug"] for h in explicit_hotels}
        missing = [s for s in slugs if s not in found]
        if missing:
            print(f"  NOTE: {len(missing)} slug(s) not found in "
                 f"v2_common_hotels, skipping: {missing}")
        if not explicit_hotels:
            raise SystemExit("none of the given slugs matched a hotel")
        city_ids = sorted({h["city_id"] for h in explicit_hotels})
        # destination/country are resolved from the FIRST matched hotel's
        # city, purely for match_hotel()'s address-stripping hint and the
        # Agoda destination lookup -- consistent with how a --city selection
        # spanning multiple city_ids (e.g. "Dubai" = 1280 AND 9658) already
        # only ever uses chosen[0] for these, not a new limitation.
        chosen = [c for c in db.resolve_city(conn, str(explicit_hotels[0]["city_id"]))
                 if c["id"] == explicit_hotels[0]["city_id"]]
        city_query = f"{len(slugs)} explicit slug(s)"
        if a.rooms_from is None:
            a.rooms_from = "both"
        print(f"\n{len(explicit_hotels)}/{len(slugs)} slug(s) matched, spanning "
             f"city_id(s) {city_ids}")
    elif a.city is None:
        chosen, city_query, a.limit, a.offset, a.rooms_from, run_hands_off = \
            _wizard(conn, led)
        # Reuses --yes's EXISTING meaning (skip the confirmation, proceed
        # automatically) rather than inventing a second flag that would mean
        # the same thing -- an operator who answered 'n' up front should get
        # exactly the behaviour someone who passed --yes on the command line
        # gets, not a parallel code path that could quietly drift from it.
        if run_hands_off:
            a.yes = True
    else:
        city_query = a.city
        matches = db.resolve_city(conn, a.city)
        if not matches:
            raise SystemExit(f"no city matching {a.city!r} in `cities`")
        if a.city_id:
            chosen = [c for c in matches if c["id"] in set(a.city_id)]
            if not chosen:
                raise SystemExit(f"--city-id {a.city_id} does not match any of "
                                 f"{[c['id'] for c in matches]}")
        else:
            print(f"\ncities matching {a.city!r}:")
            for c in matches:
                print(f"  {c['id']:>7}  {(c['name_en'] or '').title():<30} "
                      f"{c['hotels']:>6} hotels")
            if not a.yes:
                # A city name is not a city id. "Dubai" is 1280 AND 9658, and
                # `cities` also holds a row named "Brazil" carrying 6,732
                # hotels -- a country stored as a city. Neither is safe to guess.
                reply = input("\nrun against all of these? [y/N/ids] ").strip()
                if reply.lower() not in ("y", "yes"):
                    wanted = {int(x) for x in re.findall(r"\d+", reply)}
                    if not wanted:
                        raise SystemExit("aborted")
                    matches = [c for c in matches if c["id"] in wanted]
            chosen = [c for c in matches if c["hotels"]]
        if a.rooms_from is None:
            # "Bounded" only means fewer hotels -- it is not a request for a
            # smaller PIPELINE. Defaulting a --limit run to agoda-only used to
            # happen here, which meant a "quick test run" silently skipped
            # Bookme's own room names, the review/unmatched comparison, and
            # therefore silently tested a different, lesser code path than a
            # real run. `both` is the default unconditionally now; `agoda` is
            # still available, explicitly, for someone who deliberately wants
            # a fast partial pass and knows that's what they're choosing.
            a.rooms_from = "both"

    # `chosen` is filtered on hotels>0 in two branches above, so a city that
    # exists but carries no hotels empties it -- and every line below indexes
    # chosen[0]. Without this it is an IndexError traceback instead of an
    # answer to the question the operator actually asked.
    if not chosen:
        raise SystemExit(
            f"{city_query!r} matched a city, but no matching city has any "
            f"hotels in v2_common_hotels -- nothing to run.")

    # In --slugs/--slugs-file mode, city_ids was already set to the FULL
    # distinct set spanning every matched hotel's city -- `chosen` only ever
    # holds the first one (for destination/country purposes), so rebuilding
    # city_ids from it here would silently drop every other city.
    if explicit_hotels is None:
        city_ids = [c["id"] for c in chosen]
    destination = (chosen[0]["name_en"] or city_query).title()
    print(f"\nrun {run_id}: {destination} -> city_ids {city_ids}")

    # -- taxonomy ------------------------------------------------------------
    # Every setup read goes through db.with_retry from here on. These run once,
    # at startup, which is exactly when an unattended overnight run is most
    # likely to meet a database still coming back from a nightly failover --
    # and an unretried drop here killed the run before a single hotel was
    # attempted, which is strictly worse than the same drop mid-run.
    cat_ids, created, conn = _validated_cat_ids(conn)
    print(f"room categories: {len(cat_ids)} available"
          + (f", {len(created)} created this run ({', '.join(created)})"
             if created else ", none created"))

    # -- hotel set -----------------------------------------------------------
    fresh = led.fresh_ids()          # led opened once, up front, before scope
    # "Fresh" (published recently) and "done" are not the same claim. A hotel
    # some of whose rooms still have no picture is recorded as BOTH published
    # (see the ledger write after db.publish -- it really did commit) AND
    # unresolved, reason "needs_image_backfill" -- so it is excluded from the
    # skip set specifically, rather than waiting out the full staleness window
    # for a gap already known about. A hotel unresolved for any OTHER reason
    # (no_agoda_match, error, ...) was never marked published in the first
    # place, so it was never IN `fresh` to begin with -- this only ever
    # un-skips hotels that both succeeded and still owe a picture.
    skip_ids = fresh - led.unresolved_ids()
    # WASTED-WORK FIX: `db.hotels()`'s SQL has no LIMIT clause -- it fetches
    # every row for city_ids and filters/truncates in Python (see db.py) -- so
    # the full, unfiltered id set for this city was already sitting in memory
    # here and then discarded, right before a SEPARATE query (`hotel_ids()`)
    # re-fetched exactly that same id set from the same table a few lines
    # later, purely to compute `here` for the summary line below. One query
    # now serves both: `all_hotels` retains every row, `todo` is filtered from
    # it in Python instead of inside `db.hotels()`.
    #
    if explicit_hotels is not None:
        # --slugs/--slugs-file: EXACTLY these hotels, unconditionally. Naming
        # a hotel explicitly is a request to process it NOW -- silently
        # skipping it because a run happened to touch it recently would defeat
        # the entire point of naming it (a demo, a spot-check, a deliberate
        # re-run on one broken hotel).
        all_hotels = explicit_hotels
        here = {r["id"] for r in all_hotels}
        todo = all_hotels
        total_in_city = len(all_hotels)
        print(f"hotels: {len(todo)} explicit hotel(s), ledger freshness "
             f"ignored (named hotels always run)")
    else:
        # A random sample needs the full candidate list before truncating --
        # the earlier Python-side limit would otherwise hand back the lowest
        # ids, in id order, which is a biased sample (older/earlier-onboarded
        # hotels only).
        all_hotels, conn = db.with_retry(
            conn, lambda c: db.hotels(c, city_ids), what="hotel fetch")
        here = {r["id"] for r in all_hotels}
        todo = [r for r in all_hotels if r["id"] not in skip_ids]
        if a.random:
            random.shuffle(todo)
        # --offset N --limit M: the (offset+1)th through (offset+M)th hotel of
        # THIS city's own list, in the same id order --limit alone already
        # uses -- NOT a raw v2_common_hotels.id range (ids are global across
        # every city, so a literal id range would not even stay within this
        # city). Argparse already refuses --offset without --limit.
        todo = todo[a.offset:a.offset + a.limit] if a.limit else todo
        total_in_city = sum(c["hotels"] for c in chosen)
        # `fresh`/`skip_ids` are GLOBAL sets (every hotel ever published, in
        # any city). Printing their size next to this city's scope read as "N
        # hotels of THIS city were skipped" when it actually meant "N hotels
        # exist in the ledger worldwide" -- a Vienna run reported 8 skipped
        # when all 8 were in Buenos Aires and no Vienna hotel was skipped at
        # all. Scope it before printing, and count backfill-only
        # re-inclusions separately so a whole city's staleness picture is not
        # silently understated as "0 skipped".
        skipped_here = len(here & skip_ids)
        backfill_here = len(here & fresh & led.unresolved_ids())
        print(f"hotels: {total_in_city} in scope, {skipped_here} already "
             f"published within {config.LEDGER_STALE_DAYS} days (skipped), "
             f"{len(todo)} to process"
             + (f" (--offset {a.offset} --limit {a.limit})" if a.offset else
                f" (--limit {a.limit})" if a.limit else "")
             + (f"; {backfill_here} of those still needed a backfilled "
                f"image, so were included anyway" if backfill_here else ""))
    if not todo:
        raise SystemExit("nothing to do")

    # -- bookme harvest (optional) -------------------------------------------
    # The database already answers WHO exists, WHERE it is and WHAT it is
    # called, so the live API is down to one job: Bookme's own supplier room
    # NAMES, which are in no table (v2_rooms holds 11 rows for 89,015 hotels).
    #
    # Those names are now fetched PER HOTEL, keyed by the database's own slug --
    # no city search, no polling, no ref ids. The slug is the live slug (68/68
    # exact, 0 mismatches), so a "city run" is just the hotel list from the
    # database plus one call per hotel per probe shape. Measured against the old
    # city-search architecture on the same 5 hotels: +25% more rooms in 2.7x
    # less time, before counting the ~379s city harvest the old path needed
    # first. Cost is now per HOTEL and small, so it no longer has to be
    # amortised over a whole city to be worth running.
    ags = agoda.session()
    live_rooms, probe_log = {}, []
    resolved_ids, unavailable_ids = set(), set()

    # LIVE-VERIFIED GAP, closed here: a second Ctrl-C landing during the
    # Bookme harvest below (a ThreadPoolExecutor.map() blocked waiting on
    # in-flight requests) raised KeyboardInterrupt straight out of main() with
    # a raw traceback -- no report, no CSVs, no manifest, exit code 130. Only
    # the PER-HOTEL loop further down was wrapped to catch this and fall
    # through to the report section; this whole setup phase (harvest +
    # destination resolution) was not, and it is exactly where an impatient
    # second press is most likely to land, since it is the slower of the two
    # phases. `aborted_immediately` is declared here, once, and shared by both
    # try/except sites below so either one lands on the identical report path.
    aborted_immediately = False
    agoda_dead = False          # set by the circuit breaker, reported explicitly
    try:
        # What was ASKED for, kept separate from what actually ran. A degraded
        # run that recorded only its degraded mode would look identical,
        # months later, to one where the operator deliberately chose
        # agoda-only -- and the empty rooms_review.csv would be unexplainable.
        rooms_from_requested = a.rooms_from
        bookme_degraded_reason = None
        if a.rooms_from == "both":
            bs = None
            try:
                bs = bookme.session()
            except bookme.AuthFailed as e:
                # NOT fatal. The partner API has no anonymous access, so bad or
                # missing credentials end the Bookme side -- but killing the run
                # here would throw away everything Agoda can still deliver (which
                # is the imagery, i.e. the entire point) for every hotel in the
                # city. Degrade to agoda-only and say so, loudly.
                print(f"\n  Bookme partner auth failed: {e}")
                print("  Continuing with Agoda only -- rooms will carry Agoda's "
                      "naming, and rooms_review/rooms_unmatched will be empty "
                      "because there are no Bookme room names to compare against.")
                a.rooms_from = "agoda"
                bookme_degraded_reason = f"{type(e).__name__}: {e}"[:300]

            if bs is not None:
                no_slug = [h for h in todo if not (h.get("slug") or "").strip()]
                if no_slug:
                    # The slug IS the key here; a hotel without one cannot be asked
                    # about at all. Surfaced rather than silently contributing a
                    # zero indistinguishable from a hotel Bookme does not sell.
                    print(f"  NOTE: {len(no_slug)} hotel(s) have no slug in the "
                          f"database and cannot be looked up on Bookme; Agoda still "
                          f"covers them.")
                print(f"bookme: probing {len(todo) - len(no_slug)} hotel(s) by slug, "
                      f"{len(probe_shapes(config.ROOM_PROBES))} base shapes x "
                      f"{config.ROOM_PROBE_WORKERS} workers")
                try:
                    resolved_ids, unavailable_ids = harvest_rooms(
                        bs, todo, live_rooms, probe_log)
                except bookme.AuthFailed as e:
                    # Credentials died mid-pass (revoked, rotated). Everything
                    # already earned is kept -- live_rooms is mutated in place --
                    # and the rest of the run degrades rather than losing it.
                    print(f"\n  Bookme auth failed mid-probe: {e}")
                    print("  Keeping every room name already harvested; the "
                          "remaining hotels fall back to Agoda naming.")
                    a.rooms_from = "agoda" if not live_rooms else a.rooms_from
                    bookme_degraded_reason = f"{type(e).__name__}: {e}"[:300]

                found = sum(1 for v in live_rooms.values() if v)
                print(f"\nbookme: {found}/{len(todo)} hotel(s) yielded room names "
                      f"after {len(probe_log)} probe pass(es), "
                      f"{sum(len(v) for v in live_rooms.values())} room names total"
                      + (f"; {len(unavailable_ids)} reported by Bookme as not "
                         f"sellable (a stated fact, not a stopped-early zero)"
                         if unavailable_ids else ""))
        else:
            print("rooms: Agoda only (no Bookme lookup). Rooms will carry Agoda's "
                  "naming; pass --rooms-from both for supplier naming.")

    except KeyboardInterrupt:
        # LIVE-VERIFIED: a second interrupt landing here (harvest_rooms()
        # blocked on ThreadPoolExecutor futures) used to propagate raw,
        # skipping the report entirely -- exit code 130, no folder, no
        # CSVs. `live_rooms`/`probe_log` are mutated IN PLACE by
        # harvest_rooms() as it goes, so whatever was harvested before the
        # interrupt is already safely held regardless of where this landed;
        # `resolved_ids`/`unavailable_ids` keep their pre-harvest empty-set
        # default (set above) rather than being left unbound.
        aborted_immediately = True
        _STOP = True
        print("\n  aborted immediately during the Bookme/Agoda setup phase --"
              " writing whatever results were already earned before exiting")

    # -- per-hotel publish ---------------------------------------------------
    revisit, review_rows, unmatched_rows, booking_rows = [], [], [], []
    counts = {"hotels_done": 0, "rooms_inserted": 0, "attachments_inserted": 0,
              "rooms_skipped_existing": 0, "rooms_skipped_duplicate_name": 0,
              "rooms_backfilled": 0, "images_uploaded": 0,
              "rooms_published_with_no_image": 0,
              "hotels_unresolved_on_bookme": 0, "hotels_no_agoda_match": 0,
              "hotels_no_rooms": 0, "hotels_error": 0,
              "hotels_agoda_unreachable": 0,
              "rooms_with_candidate_images": 0,
              "rooms_without_candidate_images": 0,
              "rooms_filled_from_booking": 0, "hotels_helped_by_booking": 0,
              # Distinct from hotels_no_agoda_match/hotels_agoda_unreachable:
              # those two count every hotel Agoda failed on, REGARDLESS of
              # whether it went on to publish anyway via Bookme+Booking. This
              # counts only the subset that DID -- the "takeover" case (D-37).
              # Without this, the terminal-outcome buckets printed at the end
              # of a run double-count: a hotel can be BOTH "no_agoda_match" AND
              # "hotels_done" at once, and nothing said so.
              "hotels_published_without_agoda": 0}
    # hotels_mapped vs hotels_done: discovery succeeding and the DB commit
    # succeeding are different claims, now genuinely separated by phase --
    # hotels_mapped is set in Phase 1 (this hotel produced a plan, whether or
    # not Phase 2 later runs at all), hotels_done stays exactly what it always
    # meant, set only after Phase 2 actually commits it. Conflating them would
    # have broken _print_human_summary's own reconciliation check the moment a
    # hotel was mapped but a commit later failed non-transiently.
    counts["hotels_mapped"] = 0
    img_session = requests.Session()

    # The booking.com session is built ONCE, here, so its WAF token is minted
    # once for the whole run rather than per hotel. A failure to build it is not
    # fatal: Booking is a gap-filler, and losing it costs coverage on the rooms
    # Agoda already missed, not the run.
    bsess = None
    if config.BOOKING_ENABLED and not a.no_booking:
        try:
            bsess = booking.session()
            print("booking.com gap-fill: ON (waf token minted)")
        except booking.MintFailed as e:
            print(f"booking.com gap-fill: OFF -- could not mint a WAF token "
                  f"({e}). Agoda-only coverage for this run.")
    else:
        print("booking.com gap-fill: OFF (disabled)")
    check_in, check_out = stay()

    # Resolved ONCE, not per hotel. Agoda's own city id is what lets a hotel
    # with bad coordinates still match on "same city + strict name" instead of
    # being rejected outright.
    #
    # The COUNTRY CODE, though, comes from the DATABASE, not from Agoda and
    # never from config.COUNTRY_CODE. It builds the URL the browser fallback
    # navigates to, and a wrong one does not error -- the page simply never
    # asks about our property, the fallback returns 0 rooms, and the hotel is
    # recorded as having none. That is an unearned zero, the same class of
    # silent lie this pipeline exists to prevent.
    #
    # Two ways it used to be able to go wrong, both closed here:
    #   * config.COUNTRY_CODE is "ae" -- a Dubai-era default that would have
    #     built ".../vienna-ae.html" for an Austrian hotel.
    #   * agoda.resolve_destination() matches on a NAME, so "Vienna" can
    #     legitimately resolve to Vienna, Virginia and hand back "us".
    # The database knows which country the hotel is actually in, for free.
    # And if the database does NOT know, we do not guess. Falling back to
    # config.COUNTRY_CODE would just be the "ae" default wearing a different
    # hat: a URL built on a guessed country cannot land, and the browser
    # fallback would report the hotel as having no rooms -- the unearned zero
    # again, arrived at by a different route. None here means the browser
    # fallback is skipped for this run and says why, which is a smaller, honest
    # loss than a confidently wrong answer.
    db_country = (chosen[0].get("country_code") or "").strip().lower()
    country_name = (chosen[0].get("country_name") or "").strip() or None
    country_iso = db_country or None
    agoda_city_id = None
    try:
        agoda_city_id, agoda_iso = agoda.resolve_destination(ags, destination)
        print(f"agoda destination {destination} -> city {agoda_city_id} "
              f"(agoda says country {agoda_iso})")
        # Disagreement is a real signal that Agoda matched a same-named city in
        # another country; the DB still wins, but it must not pass silently.
        if agoda_iso and db_country and agoda_iso.lower() != db_country:
            print(f"  WARNING: agoda resolved {destination!r} to country "
                  f"{agoda_iso.lower()!r} but the database says {db_country!r}. "
                  f"Using the database. Agoda may have matched a same-named "
                  f"city elsewhere -- hotel matches for this run are worth "
                  f"spot-checking.")
    except KeyboardInterrupt:
        # Same live-verified gap as the harvest phase above, second site: this
        # call retries up to 5x with backoff (up to ~93s worst case) and
        # `except Exception` does NOT catch KeyboardInterrupt (it inherits
        # from BaseException) -- a second Ctrl-C landing during that backoff
        # used to propagate raw, past this whole function, with no report.
        aborted_immediately = True
        _STOP = True
        print("\n  aborted immediately while resolving the Agoda destination --"
              " writing whatever results were already earned before exiting")
    except Exception as e:
        print(f"could not resolve {destination!r} on agoda ({e}); matching "
              f"falls back to coordinates alone")
    if country_iso:
        print(f"country code for agoda urls: {country_iso!r} (from the database)")
    else:
        print("  NOTE: the database has no country code for this city, so the "
              "browser fallback is DISABLED for this run -- a guessed country "
              "builds a URL that cannot land, which would report hotels as "
              "having no rooms. HTTP and the escalation ladder still run.")

    # A SECOND Ctrl-C landing during either phase below is caught the same way
    # as the two setup-phase sites above -- `aborted_immediately` is already
    # declared, shared across all sites, so whichever one catches it lands on
    # the identical report path further down. Both loops stay inline (not
    # factored into functions) specifically so `conn` reassignments from a
    # mid-loop MySQL reconnect keep working exactly as before -- passing
    # `conn` across a function boundary would have made a rebind inside
    # invisible to this scope.
    #
    # ======================================================================
    # PHASE 1 -- DISCOVER. Match every hotel against Agoda, gap-fill against
    # Booking, decide what would be published. No image is downloaded here
    # and nothing is written to MySQL -- this phase is exactly what a
    # --dry-run has always computed, just no longer entangled with the slow
    # part. Its output is a PLAN, checkpointed per hotel (see PLAN_CACHE), so
    # a crash here loses at most the hotel in flight and a re-invocation skips
    # straight past anything already discovered.
    # ======================================================================
    plan_scope = _plan_scope(todo)
    planned_rows, planned_ids = _load_plan(plan_scope)
    if planned_ids:
        print(f"\nplan checkpoint: {len(planned_ids)} hotel(s) already "
              f"discovered, resuming")
    _save_plan_scope(plan_scope)
    # Set here, not just inside the loop below: work1 can be entirely empty
    # (every hotel in `todo` already check-pointed by a prior run), in which
    # case the loop body never executes at all and this default is what
    # survives -- correctly zero, not an unbound-variable crash.
    hotels_planned = 0
    try:
        # Same reasoning as the commit loop's own work/deferred below, one
        # stage earlier: a transient DB fault (the existing_rooms() gap-scope
        # read) earns one retry at the end of THIS phase, not an abandoned
        # hotel that already paid for its Bookme probes and Agoda ladder.
        work1 = [h for h in todo if h["id"] not in planned_ids]
        deferred1 = []
        agoda_sick = 0                 # consecutive hotels Agoda could not be ASKED about
        for i, h in enumerate(work1, 1):
            # Set on every iteration, not read back from `i` after the loop:
            # work1 can be EMPTY (every hotel in `todo` already check-pointed
            # by a prior interrupted run), in which case the loop body below
            # never executes and `i` is never bound in this scope at all --
            # unlike the single-loop original, `todo` being non-empty no
            # longer guarantees this loop runs even once.
            hotels_planned = i
            if _STOP:
                print(f"stopping discovery after {i - 1} hotels as requested")
                break
            tag = f"[{i}/{len(work1)}] {(h['name'] or '')[:38]:40}"
            revisit_row = functools.partial(_revisit_row, revisit, run_id, h,
                                            len(probe_log))
            try:
                bm_rooms = list(live_rooms.get(h["id"], {}).values())
                if a.rooms_from == "both" and h["id"] not in resolved_ids:
                    counts["hotels_unresolved_on_bookme"] += 1
                    # Bookme's own 500 is a STATED fact ("not sellable"), while a
                    # hotel we never got an answer for is an open question. Recording
                    # both as the same reason would make a permanent supplier gap
                    # indistinguishable from a network failure on the revisit list.
                    revisit_row("unresolved_on_bookme",
                                "bookme reports the property as not sellable"
                                if h["id"] in unavailable_ids
                                else "no answer from bookme on any probe shape")
                    # NOT a dead end: Agoda can still supply rooms and images for a
                    # hotel Bookme has no live offer for (roughly two thirds of the
                    # catalogue has none at any given moment).

                m = match_hotel(ags, h, check_in, check_out, city=agoda_city_id,
                                destination=destination, country_name=country_name)
                if not m.get("agoda_id"):
                    reason = m.get("reason", "")
                    # An outage and a genuine non-listing are recorded under
                    # DIFFERENT reasons, and only the outage feeds the breaker.
                    kind = ("agoda_unreachable" if m.get("unreachable")
                            else "no_agoda_match")
                    counts[f"hotels_{kind}"] += 1
                    revisit_row(kind, reason)
                    led.mark_unresolved(h, run_id, kind, reason)
                    if m.get("unreachable"):
                        agoda_sick += 1
                    else:
                        agoda_sick = 0
                    # NOT a `continue`. Agoda failing is precisely when the
                    # second source is most valuable -- Booking resolves the
                    # hotel independently, so the Bookme rooms we already
                    # harvested can still be published WITH photographs instead
                    # of the hotel being skipped outright.
                    ag_rooms, source = [], kind
                    if not bm_rooms:
                        print(f"{tag} {kind.replace('_', ' ')}, no bookme "
                              f"rooms either: {reason[:44]}")
                        continue
                    counts["hotels_published_without_agoda"] += 1
                    print(f"{tag} {kind.replace('_', ' ')} ({reason[:34]}) -- "
                          f"booking takes over for {len(bm_rooms)} room(s)")
                else:
                    agoda_sick = 0
                    ag_rooms, source = agoda_rooms(ags, m, country_iso, destination)
                    if not ag_rooms and not bm_rooms:
                        counts["hotels_no_rooms"] += 1
                        detail = f"agoda source={source}, bookme rooms=0"
                        revisit_row("no_rooms_any_date", detail)
                        led.mark_unresolved(h, run_id, "no_rooms_any_date", detail)
                        print(f"{tag} no rooms on either platform after "
                              f"{len(probe_log)} probes")
                        continue

                # ---- circuit breaker -----------------------------------------
                # The failure this exists for: Agoda blocked us mid-run and the
                # pipeline carried on for 12 more hotels writing "no agoda
                # match" -- looking healthy in the log while producing nothing.
                # An unattended city run could burn hours that way.
                if agoda_sick and agoda_sick % AGODA_SICK_ROTATE == 0:
                    ags = agoda.session()      # fresh cookies/session identity
                    cd, nxt = agoda.health()
                    print(f"\n  !! AGODA UNREACHABLE for {agoda_sick} hotels in "
                          f"a row ({cd} escalating cooldown(s), next {nxt}s). "
                          f"Rotated the session. Booking is covering these "
                          f"hotels; Agoda-only ones are on hotels_to_revisit.csv."
                          f"\n")
                if agoda_sick >= AGODA_SICK_ABORT:
                    print(f"\n  !! STOPPING: Agoda has been unreachable for "
                          f"{agoda_sick} consecutive hotels. Continuing would "
                          f"spend hours producing empty results. Everything "
                          f"discovered so far is check-pointed; re-run the same "
                          f"city later and discovery resumes where this stopped.\n")
                    agoda_dead = True
                    break

                to_publish, review, unmatched = map_rooms(
                    bm_rooms, ag_rooms, cat_ids, agoda_url=m.get("agoda_url"))

                # The existing-room snapshot is read BEFORE the gap-fill, not
                # after: a room the database already has a picture for is not a
                # gap, and asking Booking about it would buy a page (or several)
                # whose results are about to be discarded anyway -- a room this
                # informs is re-checked FRESH in Phase 2 regardless (see there),
                # this read is scoping ONLY, never trusted for what gets written.
                existing = {}
                if not a.dry_run:
                    existing, conn = db.with_retry(
                        conn, lambda c: db.existing_rooms(c, h["id"]),
                        what="existing-room check")

                # ---- second source, gaps only --------------------------------
                # Deliberately AFTER the Agoda mapping and scoped to what it
                # left empty: a hotel Agoda covered fully costs nothing here, so
                # the price of the second source is paid only where the first
                # one actually failed.
                if bsess is not None:
                    n_bk, bk_note, bk_names = booking_fill(
                        bsess, h, to_publish, cat_ids, city=destination,
                        existing=existing, log=print)
                    counts["rooms_filled_from_booking"] += n_bk
                    if n_bk:
                        counts["hotels_helped_by_booking"] += 1
                    booking_rows.append(
                        {"run_id": run_id, "hotel_id": h["id"],
                         "hotel_name": h["name"], "gaps": sum(
                             1 for r in to_publish if not r["source_images"]) + n_bk,
                         "filled": n_bk, "outcome": bk_note,
                         "rooms_filled": " ; ".join(bk_names)})

                # Counted here, from the candidate rows themselves, so it is
                # true in a DRY RUN too -- rooms_published_with_no_image is
                # derived from mirroring, which a dry run never performs, and so
                # reads 0 no matter how bad coverage actually is. This is the
                # number an A/B or a coverage check can actually compare.
                with_photo = sum(1 for r in to_publish if r["source_images"])
                without_photo = len(to_publish) - with_photo
                counts["rooms_with_candidate_images"] += with_photo
                counts["rooms_without_candidate_images"] += without_photo

                room_id = {"run_id": run_id, "city_id": h["city_id"], "city_name": destination,
                          "hotel_id": h["id"], "slug": h["slug"], "hotel_name": h["name"]}
                review_rows.extend({**room_id, **r} for r in review)
                unmatched_rows.extend({**room_id, **r} for r in unmatched)

                # ---- check-point this hotel's plan ----------------------------
                bm_label = ("n/a" if a.rooms_from != "both"
                            else f"{len(bm_rooms)}" if bm_rooms
                            else "0(unsellable)" if h["id"] in unavailable_ids
                            else "0(verified)" if h["id"] in resolved_ids
                            else "0(no answer)")
                _append_plan_rows(
                    {"id": h["id"], "city_id": h["city_id"],
                     "city_name": destination, "slug": h["slug"], "name": h["name"]},
                    to_publish, bm_label, len(ag_rooms), source,
                    len(review), len(unmatched))
                counts["hotels_mapped"] += 1
                print(f"{tag} bookme={bm_label:>10} agoda={len(ag_rooms):>2}"
                      f"({source}) -> mapped {len(to_publish)} rooms "
                      f"({with_photo} with candidate photo), {len(review)} review, "
                      f"{len(unmatched)} unmatched")
            except KeyboardInterrupt:
                raise
            except Exception as e:
                # A hotel that dies here has ALREADY paid for its Bookme probes,
                # its Agoda match and its whole escalation ladder -- the most
                # expensive work in this phase. Abandoning it on a transient
                # database fault throws all of that away and leaves a permanent
                # hole in the city. Measured: 16 hotels lost exactly this way in
                # the 2026-08-13 full-city run.
                #
                # A TRANSIENT fault earns one deferred retry at the end of this
                # phase, by which time the outage that caused it has usually
                # passed. Anything else (a real bug in mapping, a bad payload)
                # is NOT retried -- re-running it would just fail identically
                # and hide the defect behind a second identical error line.
                detail = f"{type(e).__name__}: {e}"
                transient = isinstance(e, db.TRANSIENT + (db.WriteLockTimeout,))
                if transient and h["id"] not in {x["id"] for x in deferred1}:
                    deferred1.append(h)
                    work1.append(h)         # picked up by this same loop, at the end
                    print(f"{tag} deferred after {type(e).__name__} "
                          f"(will retry at end of discovery)")
                else:
                    counts["hotels_error"] += 1
                    revisit_row("error", detail)
                    led.mark_unresolved(h, run_id, "error", detail)
                    print(f"{tag} ERROR {type(e).__name__}: {e}")
    except KeyboardInterrupt:
        aborted_immediately = True
        _STOP = True                # `global _STOP` already declared above
        print("\n  aborted immediately during discovery -- writing whatever "
              "results were already earned before exiting")

    # rooms_review/rooms_unmatched are complete the moment Phase 1 ends --
    # nothing after this point ever touches review_rows/unmatched_rows -- so
    # they are set HERE, once, rather than re-derived at report time. That
    # makes the early preview below and the final report agree by construction
    # instead of by two call sites happening to compute the same thing twice.
    counts["rooms_review"] = len(review_rows)
    counts["rooms_unmatched"] = len(unmatched_rows)

    # ==========================================================================
    # THE GATE. Everything Phase 2 needs is now either check-pointed to
    # PLAN_CACHE (this invocation's own discoveries) or was already there from
    # a prior interrupted run (planned_rows, loaded above) -- so the summary
    # below is not a forecast computed separately from Phase 2's real work, it
    # is read from the exact same counters Phase 2 will finish updating. A
    # dry run always proceeds (Phase 2 writes nothing in that mode, so there is
    # nothing to confirm); --plan-only always stops here; otherwise --yes or a
    # non-interactive stdin proceeds automatically, and an attached terminal is
    # asked.
    # ==========================================================================
    have_plan = bool(planned_ids) or counts["hotels_mapped"] > 0
    if have_plan and not aborted_immediately:
        _print_human_summary(
            {**counts, "hotels_done": counts["hotels_mapped"]},
            len(planned_ids | {h["id"] for h in todo[:hotels_planned]}),
            destination, a.rooms_from)
        print("\n(discovery only -- no image downloaded, nothing written to "
             "MySQL yet)")
    proceed = have_plan and not aborted_immediately and not a.plan_only
    if proceed and not a.dry_run and not a.yes:
        if sys.stdin.isatty():
            reply = input("\nProceed to download images and publish to MySQL? "
                          "[y/N] ").strip().lower()
            proceed = reply in ("y", "yes")
            if not proceed:
                print("stopping here -- the plan is check-pointed; re-run the "
                      "same command to pick up right where this left off")
        else:
            # No terminal to ask -- automation (cron, a CI job) must not hang
            # on input() forever. Proceeding is the SAME default --yes already
            # gives an attended run; a piped/redirected stdin is not consent
            # to skip the gate, it is simply nowhere to show it.
            print("\nno interactive terminal attached -- proceeding to Phase 2 "
                 "automatically (pass --plan-only to always stop here instead)")

    if not proceed:
        hotels_reached = hotels_planned
    else:
        # ======================================================================
        # PHASE 2 -- COMMIT. Read the plan back (this run's own discoveries plus
        # anything a prior interrupted run already check-pointed), mirror each
        # room's images, publish. Nothing here touches Agoda, Bookme or
        # Booking -- every fact it needs was already resolved in Phase 1.
        # ======================================================================
        plan_rows, plan_ids = _load_plan(plan_scope)
        # A hotel already committed by an EARLIER Phase 2 attempt (this run
        # crashed and is being resumed, or --plan-only was used and someone
        # committed part of it separately) must not be re-walked. Re-processing
        # it would be SAFE -- existing_rooms() would find nothing left to
        # mirror and db.publish() is a no-op -- but it is not FREE: a full-city
        # resume would re-query and re-print a line for every hotel already
        # done, drowning the one signal that actually matters (what's still
        # pending) under noise, which is exactly the "make me look at
        # something" experience an unattended resume must not have. Read fresh,
        # not the `fresh`/`skip_ids` computed at the top of main() -- an
        # arbitrary amount of time, and possibly an earlier Phase 2 attempt in
        # THIS same invocation's retry pass, may have published hotels since.
        already_done = _phase2_skip_ids(led)
        by_hotel = {}
        for hid, rows in plan_rows.items():
            if hid in already_done:
                continue
            first = rows[0]
            by_hotel[hid] = {
                "hotel": {"id": hid, "city_id": first["city_id"],
                         "city_name": first["city_name"], "slug": first["slug"],
                         "name": first["hotel_name"]},
                "to_publish": [_room_from_plan_row(r, cat_ids) for r in rows],
                "bm_label": first["bm_label"], "ag_count": first["ag_count"],
                "ag_source": first["ag_source"],
                "n_review": int(first["n_review"] or 0),
                "n_unmatched": int(first["n_unmatched"] or 0)}
        skipped_done = len(plan_ids) - len(by_hotel)
        if skipped_done:
            print(f"\n{skipped_done} hotel(s) in the plan are already published "
                 f"and resolved -- resuming with the remaining {len(by_hotel)}")
        work2 = list(by_hotel.values())
        deferred2 = []
        try:
            for i, entry in enumerate(work2, 1):
                if _STOP:
                    print(f"stopping commit after {i - 1} hotels as requested")
                    break
                h = entry["hotel"]
                to_publish = entry["to_publish"]
                tag = f"[{i}/{len(work2)}] {(h['name'] or '')[:38]:40}"
                revisit_row = functools.partial(_revisit_row, revisit, run_id, h,
                                                len(probe_log))
                try:
                    # Re-read fresh, never the Phase 1 snapshot: an arbitrary
                    # amount of time (and possibly another run's writes) may
                    # have passed between discovery and this commit.
                    existing = {}
                    if not a.dry_run:
                        existing, conn = db.with_retry(
                            conn, lambda c: db.existing_rooms(c, h["id"]),
                            what="existing-room check")

                    if a.dry_run:
                        uploaded = 0
                        for r in to_publish:
                            r.setdefault("thumbnail", None)
                            r.setdefault("images", [])
                        need_mirror = to_publish
                    else:
                        # WASTED-WORK FIX: mirroring used to run for every
                        # candidate room unconditionally, THEN db.publish()
                        # checked which ones were already complete and skipped
                        # them. That meant a room already fully imaged in the
                        # DB still paid a full download-from-source +
                        # upload-to-COS round trip for every one of its
                        # candidate photos, for nothing -- measured live on a
                        # backfill re-run of 2 hotels: 577 images mirrored to
                        # actually backfill 2 rooms, with 112 already-complete
                        # rooms mirrored right alongside them for no benefit at
                        # all. The fact that a room is already imaged is
                        # available BEFORE mirroring, from the exact same query
                        # db.publish() was already going to run -- so it is
                        # fetched once, above, and passed forward instead of
                        # being computed twice.
                        need_mirror, already_imaged = _split_for_mirroring(
                            to_publish, existing)
                        uploaded = mirror_all_images(need_mirror, img_session)
                        to_publish = need_mirror + already_imaged
                    counts["images_uploaded"] += uploaded
                    # A room that HAD candidate image urls but ended up with no
                    # thumbnail means every one of them failed to mirror (dead
                    # link, too small, not actually an image -- see cos.mirror).
                    # That failure is real and currently invisible: the room
                    # still gets a v2_rooms row, silently indistinguishable from
                    # case 2 (genuinely no candidate at all). Surfaced here
                    # rather than swallowed.
                    #
                    # Scoped to `need_mirror`, not the full `to_publish`: a room
                    # in `already_imaged` was deliberately never attempted (the
                    # DB says it already has a picture), which is not a mirror
                    # failure and must not be counted, logged or scheduled for
                    # revisit as one.
                    no_image_now = 0 if a.dry_run else sum(
                        1 for r in need_mirror if r["source_images"] and not r["thumbnail"])
                    counts["rooms_published_with_no_image"] += no_image_now
                    # Scheduling an early revisit (before LEDGER_STALE_DAYS) is
                    # only for a missing IMAGE, not a missing size_sqft on its
                    # own -- a revisit re-runs the full discovery cycle (Agoda
                    # suggest, geo-verify, the escalation ladder, potentially the
                    # Bookme search), real cost, to chase a secondary display
                    # field. `need_mirror` already contains every unmatched room
                    # (case 2 in the write contract -- a row with no image
                    # either), so this single pass over it is the whole
                    # imageless count; it is not `no_image_now + n_unmatched`,
                    # which would double-count them.
                    #
                    # size_sqft still backfills for free whenever a hotel IS
                    # revisited for any other reason (db.publish()'s COALESCE
                    # update fills whichever of the two fields a room is
                    # missing, independently, in the same write) -- only the
                    # SCHEDULING trigger is image-only.
                    imageless_this_hotel = sum(
                        1 for r in need_mirror if not r.get("thumbnail"))

                    if a.dry_run:
                        n_rooms, n_att, skip_old, skip_dup, backfilled = \
                            len(to_publish), 0, 0, 0, 0
                    else:
                        try:
                            n_rooms, n_att, skip_old, skip_dup, backfilled = db.publish(
                                conn, h, to_publish, existing=existing)
                        except db.TRANSIENT as e:
                            # A city run is hours long; a single dropped
                            # connection somewhere in the middle is a normal
                            # network event, not a reason to fail every hotel
                            # after it. pymysql does not auto-reconnect
                            # (ping(reconnect=True) is deprecated in this
                            # version -- confirmed, it silently does nothing),
                            # so a broken conn stays broken for every later
                            # hotel unless something replaces it.
                            #
                            # db.reconnect() retries with backoff rather than
                            # connecting once: the reconnect attempt lands
                            # during the SAME outage that broke the connection,
                            # so a single try usually fails too -- and used to
                            # leave `conn` bound to the dead socket, failing
                            # every remaining hotel in the run. Retrying the
                            # publish is safe because it is one all-or-nothing
                            # transaction that a broken connection cannot have
                            # committed, and is idempotent besides.
                            print(f"{tag}  MySQL connection dropped ({e}); "
                                  f"reconnecting and retrying this hotel once")
                            conn = db.reconnect(conn)
                            # No `existing=` here, deliberately: that snapshot
                            # was read before the outage, and correctness on
                            # this rare retry path matters more than saving one
                            # query -- let publish() re-derive it fresh from the
                            # new connection.
                            n_rooms, n_att, skip_old, skip_dup, backfilled = db.publish(
                                conn, h, to_publish)
                        led.mark_published(h, run_id, n_rooms, uploaded)
                        # Written AFTER mark_published, not instead of it: this
                        # hotel DID publish successfully (fresh_ids() should
                        # skip it next time), but it also still owes some rooms
                        # a picture, so it must NOT wait out the full 365-day
                        # staleness window like a fully-complete hotel would.
                        # Append-only + last-row-wins (see ledger.py) means this
                        # later write is what a future read sees -- exactly the
                        # ordering that makes both true at once.
                        if imageless_this_hotel:
                            led.mark_unresolved(
                                h, run_id, "needs_image_backfill",
                                f"{imageless_this_hotel} room(s) published "
                                f"without a thumbnail this run")
                    counts["rooms_inserted"] += n_rooms
                    counts["attachments_inserted"] += n_att
                    counts["rooms_skipped_existing"] += skip_old
                    counts["rooms_skipped_duplicate_name"] += skip_dup
                    counts["rooms_backfilled"] += backfilled
                    counts["hotels_done"] += 1
                    # {n_rooms} routinely EXCEEDS {ag_count}: agoda's count is
                    # distinct PHYSICAL rooms it has photos for, while a
                    # published row is one per bookme SELLABLE NAME, and bookme
                    # sells one physical room under many names (rate plans,
                    # refundable vs not, package-rate suffixes -- see D-10).
                    # Read cold in a terminal, "agoda=19 -> 58 rooms" looks like
                    # an inflated or broken count; it is 19 photo sets reused
                    # across 58 names. The note only appears when it would
                    # actually be needed to explain the gap -- the common case
                    # (n_rooms == ag_count) is silent, exactly as before.
                    ag_count = int(entry["ag_count"] or 0)
                    share_note = (
                        f" [{n_rooms} bookme names share {ag_count} agoda "
                        f"room photos -- rate-plan duplicates, not an error]"
                        if ag_count and n_rooms > ag_count else "")
                    print(f"{tag} bookme={entry['bm_label']:>10} agoda={ag_count:>2}"
                          f"({entry['ag_source']}) -> {n_rooms} rooms{share_note}, "
                          f"{uploaded} images, {entry['n_review']} review, "
                          f"{entry['n_unmatched']} unmatched"
                          + (f", {skip_old} from a previous run" if skip_old else "")
                          + (f", {skip_dup} duplicate name(s) dropped" if skip_dup else "")
                          + (f", {backfilled} backfilled" if backfilled else "")
                          + (f", {imageless_this_hotel} still need a picture"
                             if imageless_this_hotel and not a.dry_run else "")
                          # A dry run never mirrors, so every room's `thumbnail`
                          # is None and `imageless_this_hotel` is ALWAYS the
                          # full room count -- it read "26 still need a
                          # picture" even for a hotel Booking had just filled
                          # completely. That is a measurement of the dry-run
                          # mode, not of the hotel. Report the candidate-image
                          # count instead, which is the thing a dry run can
                          # actually observe.
                          + (f", {sum(1 for r in to_publish if not r['source_images'])}"
                             f" of {len(to_publish)} without a candidate image"
                             if a.dry_run else ""))
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    # A hotel that dies here has ALREADY paid for its Bookme
                    # probes, its Agoda match and its whole escalation ladder --
                    # the most expensive work in the run, and it survived Phase
                    # 1 intact. Abandoning it on a transient database fault
                    # throws all of that away and leaves a permanent hole in
                    # the city. Measured: 16 hotels lost exactly this way in the
                    # 2026-08-13 full-city run.
                    #
                    # A TRANSIENT fault earns one deferred retry at the end of
                    # this phase, by which time the outage that caused it has
                    # usually passed. Anything else (a real bug in mapping, a
                    # bad payload) is NOT retried -- re-running it would just
                    # fail identically and hide the defect behind a second
                    # identical error line.
                    detail = f"{type(e).__name__}: {e}"
                    transient = isinstance(e, db.TRANSIENT + (db.WriteLockTimeout,))
                    if transient and h["id"] not in {x["hotel"]["id"] for x in deferred2}:
                        deferred2.append(entry)
                        work2.append(entry)     # picked up by this same loop, at the end
                        print(f"{tag} deferred after {type(e).__name__} "
                              f"(will retry at end of commit)")
                    else:
                        counts["hotels_error"] += 1
                        revisit_row("error", detail)
                        led.mark_unresolved(h, run_id, "error", detail)
                        print(f"{tag} ERROR {type(e).__name__}: {e}")
        except KeyboardInterrupt:
            aborted_immediately = True
            _STOP = True             # `global _STOP` already declared above
            print("\n  aborted immediately during commit -- writing whatever "
                  "results were already earned before exiting")
        hotels_reached = i if work2 else hotels_planned
    # `hotels_reached` and `hotels_planned` are both set by this point --
    # the former by whichever of the two branches above ran, the latter right
    # after Phase 1 -- and the report section below reuses the name `i` for
    # its own, unrelated enumerate() loops over review_rows and
    # unmatched_rows, so nothing past here may read `i` expecting a hotel
    # index; that used to be exactly the kind of stale-variable bug that
    # produces a confidently wrong number instead of an error.

    # -- report --------------------------------------------------------------
    # Sorts chronologically (run_id leads) AND reads at a glance months later:
    # date-time, which city_id(s), and whether it was bounded or the whole
    # city -- the three things you'd otherwise have to open manifest.json to
    # learn. "unbound" rather than a number for a whole-city run, so an empty
    # --limit isn't misread as a typo'd id.
    bound_label = "unbound" if not a.limit else f"limit{a.limit}"
    city_label = "+".join(str(i) for i in city_ids)
    # -STOPPED is visible in a plain `ls out/runs/`, not just inside
    # manifest.json's "stopped_early" field -- the whole point is telling a
    # partial run apart from a completed one without opening anything.
    stop_label = ("-AGODA-DOWN" if agoda_dead else "-STOPPED" if _STOP else
                 "-PLAN-ONLY" if not proceed else "")
    folder = os.path.join(
        OUT, "runs", f"{run_id}-city{city_label}-{bound_label}{stop_label}")
    os.makedirs(folder, exist_ok=True)
    _write_csv(os.path.join(folder, "hotels_to_revisit.csv"), HOTEL_COLUMNS, revisit)
    # Highest score first: the review queue is manual human labor, and a
    # near-certain pairing (74%, one word different) is a much faster "yes"
    # than a genuinely 50/50 one -- ordering by confidence clears the easy
    # majority first instead of making a reviewer hunt for them in hotel order.
    review_rows.sort(key=lambda r: -r["score"])
    # row_id: a stable handle to refer to one row by ("row 7"), not derivable
    # from any other column here (unlike the ledger, a hotel/room name pair
    # isn't unique across a whole run's rooms). Assigned once, at write time,
    # in the same order the row was produced -- apply_review_decisions reads
    # it straight back off the CSV, it never needs to be recomputed.
    for i, r in enumerate(review_rows, 1):
        r["row_id"] = i
    for i, r in enumerate(unmatched_rows, 1):
        r["row_id"] = i
    _write_csv(os.path.join(folder, "rooms_review.csv"), REVIEW_COLUMNS, review_rows)
    _write_csv(os.path.join(folder, "rooms_unmatched.csv"), UNMATCHED_COLUMNS, unmatched_rows)
    # One line per hotel the gap-fill was ASKED about, including the ones it
    # could not help -- "outcome" distinguishes unresolved identity from no
    # rooms from no match, which a bare filled-count would flatten into a
    # single uninformative zero.
    _write_csv(os.path.join(folder, "booking_fill.csv"), BOOKING_COLUMNS, booking_rows)
    counts["rooms_review"] = len(review_rows)
    counts["rooms_unmatched"] = len(unmatched_rows)
    with open(os.path.join(folder, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({
            "run_id": run_id, "city": destination, "city_ids": city_ids,
            "country_code": country_iso,
            "dry_run": a.dry_run, "limit": a.limit,
            # requested vs effective: a run degraded to agoda-only must not be
            # indistinguishable from one where agoda-only was chosen on purpose
            "rooms_from_requested": rooms_from_requested,
            "rooms_from_effective": a.rooms_from,
            "bookme_degraded_reason": bookme_degraded_reason,
            # Same requested-vs-effective discipline as rooms_from: a run whose
            # token could not be minted must not read as a run that chose
            # Agoda-only. Without this, a silent loss of the second source shows
            # up only as slightly worse coverage months later.
            "booking_requested": bool(config.BOOKING_ENABLED and not a.no_booking),
            "booking_effective": bsess is not None,
            # Two-phase transparency: a manifest from a plan-only or
            # gate-declined invocation must not be mistaken for a completed
            # run just because it has counts and a folder like one. `proceed`
            # is the single source of truth for whether Phase 2 ran at all.
            "plan_only_requested": a.plan_only,
            "phase2_ran": proceed,
            "hotels_planned": hotels_planned,
            "started_at": datetime.datetime.fromtimestamp(
                started, datetime.timezone.utc).isoformat(timespec="seconds"),
            "duration_s": round(time.time() - started, 1),
            "stopped_early": _STOP,
            # False when the loop finished naturally OR stopped gracefully
            # (finished the hotel in flight); True only when a second
            # interrupt forced an immediate exit -- the hotel in flight, if
            # any, may not have completed. Every hotel that reached
            # `led.mark_published` either way is real, committed work
            # regardless of which of these this is.
            "aborted_immediately": aborted_immediately,
            # A run cut short by the breaker is NOT a finished run, and must
            # never read as one: without this, "37 of 50 hotels" and a healthy
            # exit code look identical to a clean, complete pass.
            "stopped_by_agoda_breaker": agoda_dead,
            "hotels_in_scope": total_in_city, "hotels_attempted": len(todo),
            # scoped to THIS city, not the global ledger size -- see above
            "hotels_skipped_fresh": skipped_here,
            "hotels_in_ledger_globally": len(fresh),
            "probes": probe_log, "counts": counts,
            "config": {k: getattr(config, k) for k in (
                "ADULTS", "STAY_WEEKS_OUT", "STAY_NIGHTS", "ROOM_ACCEPT",
                "ROOM_REVIEW", "ROOM_PROBES", "ROOM_PROBES_ESCALATION",
                "PROBE_MIDWEEK_OFFSET_DAYS", "ROOM_PROBE_WORKERS",
                "TRUST_PERMANENT_UNAVAILABLE", "MAX_IMAGES_PER_ROOM",
                "LEDGER_STALE_DAYS", "CACHE_FRESH_DAYS",
                "REVIEW_BAND_CREATES_ROOM", "BOOKING_ENABLED",
                "BOOKING_PROBES", "BOOKING_MIN_NAME", "BOOKING_MAX_KM",
                "BOOKING_GEO_CANDIDATES")},
            # Which environment answered. The slug namespaces differ between
            # UAT and prod, so a run whose API and database disagreed would look
            # normal here without it -- just quietly missing most hotels.
            "bookme_api_base": bookme.api_base(),
            "room_categories": cat_ids,
        }, f, indent=2, ensure_ascii=False)

    # A one-glance human summary for exactly the situation that motivated it:
    # watching a run struggle (too many hotels missed, connections dropping
    # repeatedly) and wanting to stop and SEE what happened, not dig through
    # JSON. Written ONLY for a stopped run -- a completed run's numbers are
    # already in the console output and don't need a second copy.
    if _STOP:
        reason_counts = collections.Counter(r["reason"] for r in revisit)
        summary_lines = [
            f"Run {run_id} ({destination}) was stopped before finishing.",
            "",
            ("Aborted immediately (second interrupt) -- the hotel in "
             "flight, if any, may not have completed."
             if aborted_immediately else
             "Stopped gracefully -- the hotel in flight finished and "
             "committed before exiting."),
            "",
            f"Hotels reached: {hotels_reached} of {len(todo)} selected this "
            f"run ({total_in_city} total in scope).",
            f"Successfully published: {counts['hotels_done']}",
            "",
        ]
        if reason_counts:
            summary_lines.append("Missed hotels, by reason (see "
                                 "hotels_to_revisit.csv for the full list "
                                 "with hotel names and details):")
            for reason, n in reason_counts.most_common():
                summary_lines.append(f"  {n:>4}  {reason}")
            summary_lines.append("")
        summary_lines += [
            "To continue: re-run the same city. Already-published hotels "
            "are skipped automatically (the ledger), and hotels listed "
            "above as missed are retargeted first -- nothing needs to be "
            "picked manually.",
            "",
            "    python -m pipeline.run --city " + destination,
            "",
            "Full detail: manifest.json, hotels_to_revisit.csv, "
            "rooms_review.csv, rooms_unmatched.csv, all in this folder.",
        ]
        with open(os.path.join(folder, "STOPPED_RUN_SUMMARY.txt"), "w",
                 encoding="utf-8") as f:
            f.write("\n".join(summary_lines) + "\n")

    # The Agoda property cache and the discovery plan are mid-run scaffolding,
    # not a deliverable -- they exist so a hotel already verified earlier in
    # THIS run (or a run crashed and resumed) isn't re-discovered, never as a
    # second source of truth to keep around. A run that reached the end of its
    # hotel list AND committed it is "done" in the sense that matters here, so
    # the cache is swept -- MySQL and the ledger are now the record. Anything
    # short of that (Ctrl-C, the Agoda breaker, the gate declined, or
    # --plan-only) is NOT done -- it is paused, and the whole point of the
    # cache is to make resuming it cheap without re-discovering anything, so it
    # is left alone. `proceed` being False means Phase 2 never ran at all, in
    # which case the plan is the ONLY record of this run's discovery -- wiping
    # it would silently throw away exactly the work the operator just chose to
    # hold onto for later.
    if proceed and not (_STOP or agoda_dead):
        shutil.rmtree(CACHE, ignore_errors=True)

    status = ("STOPPED (agoda unreachable -- circuit breaker)" if agoda_dead else
              "STOPPED (aborted immediately)" if aborted_immediately else
              "STOPPED (finished the hotel in flight)" if _STOP else
              "PLAN ONLY -- nothing committed" if not proceed else
              "finished")
    cache_cleared = proceed and not (_STOP or agoda_dead)
    print(f"\nrun {run_id} {'(DRY RUN) ' if a.dry_run else ''}{status} in "
          f"{(time.time() - started) / 60:.1f} min"
          + (" -- cache cleared" if cache_cleared else ""))
    if proceed:
        # hotels_reached, not len(todo): a stopped-early run must be
        # summarised against what it actually attempted, or the funnel below
        # would show a phantom shortfall and flag itself as a false
        # accounting bug.
        _print_human_summary(counts, hotels_reached, destination, a.rooms_from)
    else:
        # The gate already printed this exact summary once, moments ago, with
        # hotels_mapped standing in for hotels_done because nothing had been
        # committed yet. Reprinting it here with the REAL counts would show
        # "0 published" right under a summary that just said otherwise --
        # correct, since Phase 2 genuinely never ran, but reads as a
        # regression rather than the deliberate stop it is.
        print(f"\n{hotels_planned} hotel(s) discovered and check-pointed, "
             f"0 committed (Phase 2 did not run).")
    print(f"\nFull machine-readable numbers: {os.path.join(folder, 'manifest.json')}")
    print(f"  report -> {folder}")
    if _STOP:
        print(f"  summary -> {os.path.join(folder, 'STOPPED_RUN_SUMMARY.txt')}")
        print(f"  to continue: python -m pipeline.run --city {destination}")
    elif not proceed:
        print(f"  to commit the check-pointed plan: "
             f"python -m pipeline.run --city {destination}")
    conn.close()
    if lock is not None:
        lock.close()               # releases the flock; the OS also does this
                                   # on any exit path, crash included


if __name__ == "__main__":
    main()
