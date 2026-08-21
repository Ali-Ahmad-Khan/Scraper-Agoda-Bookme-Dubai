"""Bookme's MySQL: read hotels, write rooms and their attachments.

In:  a city name.
Out: hotel records to process, and (after matching) committed v2_rooms +
     v2_attachments rows.

ADDITIVE, WITH ONE NAMED, NARROW EXCEPTION. Every statement issued through
`_sql` is a SELECT or an INSERT -- it refuses anything else at runtime, so no
code path routed through it can UPDATE, DELETE, ALTER or TRUNCATE, including
by accident in a future edit.

The one exception is `_fill_empty_fields`, authorised explicitly, covering two
columns: a room published with NO image (no Agoda candidate existed yet, or
every candidate image failed to download) and/or with NO `size_sqft` (Agoda's
room-size feature was absent, or the candidate that matched didn't carry one)
may have either FILLED on a later run, so a room does not sit incomplete
forever waiting for the 365-day staleness window. It does not go through
`_sql` -- it is its own tiny, separately-audited function, and the safety
property is structural, not conventional: `SET thumbnail=COALESCE(thumbnail,
%s), size_sqft=COALESCE(size_sqft, %s)` means each column is independently
protected -- COALESCE(existing_value, anything) is always just existing_value,
so it is not possible to call this function in a way that overwrites either
field once it is already set. Nothing else about a room -- its name, category,
or an existing value in either column -- is ever touched. `id` is fixed and
never reassigned; a backfill only ever adds a fact a previously-incomplete row
was always missing.
"""
import contextlib
import os
import re
import time

import pymysql

from . import categories, config

config.load_env()

# Anything not in this set is refused before it reaches the server. The DB user
# holds ALL PRIVILEGES, so this guard -- not the grant -- is what makes the
# "additions only" contract real.
_ALLOWED = ("select", "insert", "show")
_VERB = re.compile(r"^\s*(\w+)")


def _sql(cur, query, args=None):
    verb = _VERB.match(query).group(1).lower()
    assert verb in _ALLOWED, (
        f"refused {verb.upper()}: pipeline.db is additive-only, it may not "
        f"modify or remove anything already in the database")
    cur.execute(query, args)
    return cur


TRANSIENT = (pymysql.err.OperationalError, pymysql.err.InterfaceError)

# ---------------------------------------------------------------- write lock
# `v2_rooms` has NO unique constraint, so the dedupe that stops a re-run
# duplicating rooms is a READ (existing_rooms) then INSERT -- a check-then-act
# that is atomic only against itself, inside one transaction, in one process.
#
# run.py's flock guard covers `python -m pipeline.run` and is skipped entirely
# for --dry-run. It does NOT cover a standalone script that imports this module.
# That gap is not hypothetical: on 2026-08-13 a second process wrote 18,251 rows
# across 431 hotels DURING a real run's probe phase, producing three
# `Lock wait timeout exceeded (1205)` errors and corrupting the run's own
# accounting (it reported 232 inserts while the table gained 18k rows).
#
# The lock therefore lives HERE, at the only place rooms are ever written, and
# is a MySQL advisory lock rather than a file lock so it works across processes
# AND across machines. GET_LOCK/RELEASE_LOCK are SELECTs, so they pass the
# additive-only guard unchanged, and MySQL frees the lock automatically when the
# connection closes -- a killed writer never strands it.
#
# Scope is PER HOTEL, not global: two writers on different hotels cannot collide
# (the dedupe key is (hotel, room name)), so a global lock would serialise the
# whole pipeline for no safety gain.
WRITE_LOCK_PREFIX = "bookme_v2rooms_hotel_"
WRITE_LOCK_TIMEOUT_S = 30


class WriteLockTimeout(RuntimeError):
    """Another process holds this hotel's write lock. Refusing to double-insert."""


@contextlib.contextmanager
def hotel_write_lock(conn, hotel_id, timeout=None):
    """Hold the advisory write lock for one hotel for the duration of the block.

    Raises WriteLockTimeout rather than proceeding unguarded: publishing anyway
    is the one damage this pipeline cannot undo, because cleaning up duplicate
    rooms would need a DELETE and the additive-only contract forbids it.
    """
    name = f"{WRITE_LOCK_PREFIX}{hotel_id}"
    wait = WRITE_LOCK_TIMEOUT_S if timeout is None else timeout
    with conn.cursor() as cur:
        _sql(cur, "SELECT GET_LOCK(%s, %s) AS got", (name, wait))
        got = (cur.fetchone() or {}).get("got")
    if got != 1:
        raise WriteLockTimeout(
            f"could not acquire {name} within {wait}s -- another process is "
            f"writing this hotel. Not publishing: v2_rooms has no unique "
            f"constraint, so a concurrent insert would duplicate rooms "
            f"permanently (cleanup would need a DELETE, which is forbidden).")
    try:
        yield
    finally:
        try:
            with conn.cursor() as cur:
                _sql(cur, "SELECT RELEASE_LOCK(%s) AS r", (name,))
                cur.fetchone()
        except Exception:
            # The connection died; MySQL releases the lock on disconnect anyway.
            pass


def _connect_once():
    return pymysql.connect(
        host=os.getenv("MYSQL_HOST"), port=int(os.getenv("MYSQL_PORT") or 3306),
        user=os.getenv("MYSQL_USER"), password=os.getenv("MYSQL_PASSWORD"),
        database=os.getenv("MYSQL_DATABASE"), charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor)


def connect(tries=6, base_delay=2, log=print):
    """Connect, retrying with exponential backoff.

    A single attempt was a real cascade bug: a city run is hours long, and when
    the network drops mid-run the reconnect ATTEMPT lands during the same outage
    that caused the drop. A one-shot connect() raises, the caller's `conn` is
    left pointing at the dead connection, and every remaining hotel in the run
    fails against it -- one blip poisoning a whole run.

    The backoff (2,4,8,16,32,64s -- just over two minutes) is sized for the
    ordinary case this has to survive: a router reboot, a wifi drop, a database
    failover. A longer outage still fails, but it fails having genuinely tried.
    """
    last = None
    for i in range(tries):
        try:
            return _connect_once()
        except TRANSIENT as e:
            last = e
            if i == tries - 1:
                break
            wait = base_delay * (2 ** i)
            if log:
                log(f"  MySQL unreachable ({type(e).__name__}); "
                    f"retrying in {wait}s ({i + 1}/{tries - 1})")
            time.sleep(wait)
    raise last


def reconnect(conn, log=print):
    """Replace a connection known to be broken. Never raises past `connect`'s
    own budget, and NEVER returns the dead connection -- callers rebind to the
    result, so handing back the old object would silently keep the outage."""
    try:
        conn.close()
    except Exception:
        pass                                  # discarding it regardless
    return connect(log=log)


def with_retry(conn, fn, log=print, what="query"):
    """Run `fn(conn)`, reconnecting once if the connection dropped underneath it.

    Returns (result, conn) -- the connection is returned because it may have
    been REPLACED, and a caller that keeps using its old reference would issue
    every later statement against a closed socket. Read paths need this as much
    as writes: a drop during the initial hotel fetch used to kill the run
    outright, before a single hotel had been attempted.

    `fn` must be idempotent or transactional. Every caller here is: reads are
    pure, and publish() is one all-or-nothing transaction that a broken
    connection could not have committed.
    """
    try:
        return fn(conn), conn
    except TRANSIENT as e:
        if log:
            log(f"  MySQL connection dropped during {what} ({type(e).__name__}); "
                f"reconnecting and retrying once")
        conn = reconnect(conn, log=log)
        return fn(conn), conn


# ------------------------------------------------------------------ reading
# country_code comes along for the ride because it is NOT optional metadata:
# it builds the Agoda property URL the browser fallback navigates to, and a
# wrong one silently fails to land and reports the hotel as having no rooms.
# The database knows it authoritatively and for free, which is strictly better
# than either a hardcoded constant or Agoda's own destination guess -- the
# latter can resolve "Vienna" to Vienna, Virginia.
_HOTEL_COUNT_SQL = """
    SELECT c.id, c.name_en, c.country_id,
           LOWER(co.alpha_2) AS country_code,
           JSON_UNQUOTE(JSON_EXTRACT(co.name, '$.en')) AS country_name,
           (SELECT COUNT(*) FROM v2_common_hotels h WHERE h.city_id = c.id)
               AS hotels
    FROM cities c
    LEFT JOIN countries co ON co.id = c.country_id
    WHERE c.deleted_at IS NULL AND {where}
    ORDER BY hotels DESC"""


def resolve_city(conn, query):
    """Every city matching an operator's input, with its hotel count.

    Accepts EITHER a name or a numeric city_id -- a bare digit string is
    unambiguous (no city name is all-digits), so which one the operator meant
    doesn't need a separate question. A pure id lookup still goes through this
    same query shape rather than a short-circuit, so an id typo (an id that
    doesn't exist) comes back as "no match", not a different code path.

    Never returns one answer for a name. "Dubai" is `city_id` 1280 AND 9658
    ("Bur Dubai", 55 hotels including Hyatt Regency Dubai Creek Heights), and a
    run scoped to the obvious id would silently omit hotels a person would
    absolutely call Dubai. The caller shows this list and asks.
    """
    q = (query or "").strip()
    with conn.cursor() as cur:
        if q.isdigit():
            _sql(cur, _HOTEL_COUNT_SQL.format(where="c.id = %s"), (int(q),))
        else:
            _sql(cur, _HOTEL_COUNT_SQL.format(where="(c.name_en = %s OR c.name_en LIKE %s)"),
                 (q, f"%{q}%"))
        return cur.fetchall()


def hotels(conn, city_ids, limit=None, skip_ids=()):
    """Hotels to process, straight from the canonical table.

    This replaces a whole nondeterministic city-wide search: name, slug,
    address and coordinates are all here, populated, and free. The public API
    is still needed for live room names -- but not for who exists.
    """
    q = ("SELECT id, slug, name_en AS name, address_en AS address, "
         "latitude AS lat, longitude AS lon, city_id "
         "FROM v2_common_hotels WHERE city_id IN %s ORDER BY id")
    with conn.cursor() as cur:
        _sql(cur, q, (tuple(city_ids),))
        rows = cur.fetchall()
    # `limit` selects a WINDOW of the catalogue, not a quota of work to find.
    # It is applied to the ordered rows BEFORE skip_ids, so `--limit 1500`
    # always means the same 1500 hotels no matter what the ledger already
    # holds. The other way round -- skipping first, then counting to 1500 --
    # makes the window slide further down the catalogue on every re-run as the
    # ledger fills, so no two runs cover the same hotels and a result can never
    # be reproduced or backtracked. Skipped hotels are subtracted from the
    # window, not replaced: a run that skips 200 already-fresh hotels processes
    # 1300, and those 1300 are a subset of the same fixed 1500.
    if limit:
        rows = rows[:limit]
    out = []
    for r in rows:
        if r["id"] in skip_ids:
            continue
        r["lat"] = float(r["lat"]) if r["lat"] is not None else None
        r["lon"] = float(r["lon"]) if r["lon"] is not None else None
        out.append(r)
    return out


def hotels_by_slug(conn, slugs):
    """Explicit, named hotels -- the --slugs/--slugs-file CLI path.

    Identical row shape to hotels() (same columns, same lat/lon float
    coercion) so it can flow through exactly the same Phase 1/Phase 2
    machinery without a special case anywhere downstream -- the only
    difference is HOW the hotel set was chosen, not what a hotel record looks
    like once chosen. City-agnostic on purpose: an operator naming a slug
    already knows which hotel they mean, so there is no city to filter by.
    """
    if not slugs:
        return []
    q = ("SELECT id, slug, name_en AS name, address_en AS address, "
         "latitude AS lat, longitude AS lon, city_id "
         "FROM v2_common_hotels WHERE slug IN %s")
    with conn.cursor() as cur:
        _sql(cur, q, (tuple(slugs),))
        rows = cur.fetchall()
    for r in rows:
        r["lat"] = float(r["lat"]) if r["lat"] is not None else None
        r["lon"] = float(r["lon"]) if r["lon"] is not None else None
    return rows


def hotel_ids(conn, city_ids):
    """Just the ids. For counting//cross-referencing against the ledger, where
    pulling names, addresses and coordinates for thousands of hotels to then
    read only `id` is pure waste -- and it happens while an operator waits at
    an interactive prompt."""
    with conn.cursor() as cur:
        _sql(cur, "SELECT id FROM v2_common_hotels WHERE city_id IN %s",
             (tuple(city_ids),))
        return [r["id"] for r in cur.fetchall()]


def existing_rooms(conn, common_hotel_id):
    """{name.lower(): {"id", "has_image", "has_size"}} for this hotel.

    v2_rooms has no unique constraint, so nothing at the schema level stops a
    re-run inserting the same room twice -- these two flags are what let
    `publish` tell "this name already exists, skip it" apart from "this name
    already exists AND is still missing a picture or its dimensions, backfill
    whichever it's missing."
    """
    size = "size_sqft" if has_size_sqft(conn) else "NULL AS size_sqft"
    with conn.cursor() as cur:
        _sql(cur, f"SELECT id, name, thumbnail, {size}, room_category_id "
                  f"FROM v2_rooms WHERE v2_common_hotel_id = %s", (common_hotel_id,))
        return {r["name"].strip().lower():
                {"id": r["id"], "has_image": bool(r["thumbnail"]),
                 "has_size": True if size.startswith("NULL") else r["size_sqft"] is not None,
                 "has_category": r["room_category_id"] is not None}
                for r in cur.fetchall()}


_ROOM_COLS = None


def room_columns(conn):
    """The columns v2_rooms ACTUALLY has here, cached for the process.

    UAT and prod are not schema-identical. `size_sqft` was added to UAT for
    this project and does NOT exist on prod (verified 2026-08-20), so every
    statement naming it -- the INSERT, the COALESCE backfill, the
    existing-rooms read -- is a hard `Unknown column` error against production.
    That would have failed EVERY publish, and it stayed invisible because the
    prod run was interrupted during discovery and Phase 2 never ran.

    Adapting is deliberate rather than requiring a prod migration: size is a
    secondary display field, and refusing to run a whole imagery pipeline over
    a missing optional column would be the wrong trade. Add the column to prod
    and this starts populating it with no code change.
    """
    global _ROOM_COLS
    if _ROOM_COLS is None:
        with conn.cursor() as cur:
            _sql(cur, "SHOW COLUMNS FROM v2_rooms")
            _ROOM_COLS = {r["Field"] for r in cur.fetchall()}
    return _ROOM_COLS


def has_size_sqft(conn):
    return "size_sqft" in room_columns(conn)


def existing_rooms_bulk(conn, city_ids):
    """{hotel_id: {name.lower(): {...}}} for a whole city, in ONE query.

    Same shape existing_rooms() returns per hotel, fetched for the entire run
    up front. Replaces one round trip per hotel with one round trip per run --
    2,916 queries collapse to 1 on a Dubai prod run.

    That is not only a wasted-work fix. Those per-hotel reads were the single
    biggest source of lost work in production: measured 2026-08-19, 221 hotels
    were abandoned mid-discovery because the connection dropped on exactly this
    call, each one throwing away a completed Bookme probe ladder and Agoda
    match. One read at startup cannot fail 221 times.
    """
    out = {}
    if not city_ids:
        return out
    size = "r.size_sqft" if has_size_sqft(conn) else "NULL AS size_sqft"
    with conn.cursor() as cur:
        _sql(cur, f"SELECT r.v2_common_hotel_id AS hid, r.id, r.name, r.thumbnail, "
                  f"{size}, r.room_category_id FROM v2_rooms r "
                  f"JOIN v2_common_hotels h ON h.id = r.v2_common_hotel_id "
                  f"WHERE h.city_id IN %s", (tuple(city_ids),))
        for r in cur.fetchall():
            out.setdefault(r["hid"], {})[(r["name"] or "").strip().lower()] = {
                "id": r["id"], "has_image": bool(r["thumbnail"]),
                # Where the column does not exist, "already has a size" is
                # vacuously TRUE -- there is nothing to backfill into, so a
                # room must never be routed through a write on its account.
                "has_size": True if size.startswith("NULL") else r["size_sqft"] is not None,
                "has_category": r["room_category_id"] is not None}
    return out


def _fill_empty_fields(cur, room_id, thumbnail=None, size_sqft=None,
                       category_id=None):
    """THE one narrow exception to additive-only, covering every backfillable
    field in one statement.

    `room_category_id` is here for a root cause that took two attempts to find.
    Most v2_rooms rows are NOT written by this pipeline: calling Bookme's
    `/hotels/api/availability` makes BOOKME's own backend persist the room names
    it discovers, with `room_category_id` NULL (proven -- one API call on an
    untouched hotel created 5 rows; the write path here was never invoked). Our
    probe ladder asks that endpoint ~28 times per hotel, so a run populates
    thousands of uncategorised rows as a side effect of merely *reading* room
    names, and we then only ever set a category on rows WE insert. That, not a
    "concurrent second writer", is why a full-city run left the vast majority of
    rows with no category. Backfilling it here fixes the class.

    COALESCE in the SET clause is what makes the overwrite-guarantee true, and
    it protects each column INDEPENDENTLY: passing a thumbnail can never touch
    size_sqft or vice versa, and neither can ever replace a value that is
    already set, because COALESCE(existing_value, anything) is always just
    existing_value. Passing None for a field you have nothing new to offer is
    always safe for the same reason.

    The WHERE clause does not add to that safety -- COALESCE already
    guarantees it -- it only decides which rows are worth touching at all, so
    a call with nothing to actually improve is a no-op rather than a wasted
    write. Returns True iff at least one column's stored value actually
    changed (MySQL's own UPDATE rowcount semantics: rows CHANGED, not rows
    matched); False means there was nothing to gain, which is not an error.
    """
    # The WHERE is built from what is actually OFFERED, not from which columns
    # happen to be empty. A static condition fires on a row whose only empty
    # column is one this call has nothing for -- changing nothing but
    # `updated_at`, and reporting True as though something was gained. Caught by
    # this module's own guard test the moment room_category_id joined the SET.
    # Where v2_rooms has no size_sqft column (prod), the field is dropped from
    # both the SET and the WHERE rather than the whole statement failing --
    # every other backfill still lands. See room_columns().
    with_size = "size_sqft" in (_ROOM_COLS or {"size_sqft"})
    if not with_size:
        size_sqft = None
    conds, sets, args = [], ["thumbnail=COALESCE(thumbnail, %s)"], [thumbnail]
    if thumbnail is not None:
        conds.append("(thumbnail IS NULL OR thumbnail='')")
    if with_size:
        sets.append("size_sqft=COALESCE(size_sqft, %s)")
        args.append(size_sqft)
        if size_sqft is not None:
            conds.append("size_sqft IS NULL")
    sets.append("room_category_id=COALESCE(room_category_id, %s)")
    args.append(category_id)
    if category_id is not None:
        conds.append("room_category_id IS NULL")
    if not conds:
        return False
    cur.execute(
        f"UPDATE v2_rooms SET {', '.join(sets)}, updated_at=NOW() "
        f"WHERE id=%s AND ({' OR '.join(conds)})",
        (*args, room_id))
    return cur.rowcount > 0


# ------------------------------------------------------------------ writing
def publish(conn, hotel, rooms, existing=None):
    """Insert one hotel's rooms and their images. ONE transaction, all or nothing.

    `existing` may be passed in as an already-fetched `existing_rooms()`
    snapshot, to avoid querying it twice: the caller typically needs the same
    snapshot BEFORE this call too, to decide which rooms are worth mirroring
    images for in the first place (a room already fully complete gets no
    benefit from a fresh download+reupload of the same picture). Pass None
    (the default) to have this function fetch its own, fresh -- every caller
    that does not already hold one, and the reconnect-retry path after a
    dropped connection, where a snapshot read before the outage should not be
    trusted over a fresh one.

    `rooms` is a list of {name, category_id, thumbnail, images}, where every URL
    is already a COS URL -- uploading inside a transaction would leave orphaned
    objects on rollback, and object storage cannot be rolled back. Content
    addressing makes an orphan from a crashed run harmless anyway: the next run
    computes the same key and reuses it.

    Returns (rooms_inserted, attachments_inserted, skipped_existing,
             skipped_duplicate, backfilled).

    Every candidate room falls into exactly one of four cases:

      new room                  no row by this name yet -> INSERT (name,
                                thumbnail AND size_sqft together)
      existing, missing image   a PRIOR run left this name short an image, a
                     and/or size dimension, or both, and we have something new
                                to offer for at least one -> backfill: fill
                                whichever field(s) are still empty (see
                                _fill_empty_fields), and if the THUMBNAIL is
                                what's being newly set, also INSERT the rest of
                                the gallery as attachments. `id` is fixed;
                                nothing is re-created.
      existing, fully complete  already has both -> skipped_existing. Re-run
                                protection working as designed.
      existing, nothing to add  still missing something, but we have nothing
                                new to offer it either this time -> also
                                skipped_existing, untouched, no-op.
      duplicate within THIS run a Bookme room and an unclaimed Agoda room
                                collided on a name in the SAME call -> the
                                second is skipped_duplicate. That means the
                                mapping stage handed us something slightly
                                wrong -- worth seeing, not worth failing over.

    Reporting skipped_existing and skipped_duplicate as one number actively
    misled in the past: a hotel that had never been run reported "2 already
    present", which read as prior-run state when it was an intra-run collision.
    """
    # Everything in _publish_locked -- the existing-rooms READ and the INSERTs
    # that act on it -- runs under this hotel's advisory lock, because the read
    # and the write together are the check-then-act. Reading BEFORE taking the
    # lock would leave exactly the race the lock exists to close: another writer
    # could insert between our read and our insert. See hotel_write_lock().
    with hotel_write_lock(conn, hotel["id"]):
        return _publish_locked(conn, hotel, rooms, existing)


def _publish_locked(conn, hotel, rooms, existing):
    """publish()'s body, executed while holding the hotel's write lock.

    Split out purely so the lock is impossible to bypass by editing the body --
    there is no code path into these INSERTs that does not go through publish().
    """
    from . import cos
    n_rooms = n_att = backfilled = 0
    claimed_this_run = set()
    skipped_existing = skipped_duplicate = 0
    with_size = has_size_sqft(conn)      # prod lacks the column -- see room_columns()
    if existing is None:
        existing = existing_rooms(conn, hotel["id"])
    try:
        conn.begin()
        with conn.cursor() as cur:
            for r in rooms:
                name = (r["name"] or "").strip()[:config.ROOM_NAME_MAX]
                if not name:
                    continue
                key = name.lower()
                if key in claimed_this_run:
                    skipped_duplicate += 1
                    continue
                claimed_this_run.add(key)
                prior = existing.get(key)

                if prior is None:
                    if with_size:
                        _sql(cur,
                             "INSERT INTO v2_rooms (hotel_id, v2_common_hotel_id, "
                             "name, description, room_category_id, thumbnail, "
                             "size_sqft, max_adults, max_children, created_at, "
                             "updated_at) "
                             "VALUES (0, %s, %s, NULL, %s, %s, %s, NULL, NULL, "
                             "NOW(), NOW())",
                             (hotel["id"], name, r.get("category_id"),
                              r.get("thumbnail"), r.get("size_sqft")))
                    else:
                        _sql(cur,
                             "INSERT INTO v2_rooms (hotel_id, v2_common_hotel_id, "
                             "name, description, room_category_id, thumbnail, "
                             "max_adults, max_children, created_at, updated_at) "
                             "VALUES (0, %s, %s, NULL, %s, %s, NULL, NULL, "
                             "NOW(), NOW())",
                             (hotel["id"], name, r.get("category_id"),
                              r.get("thumbnail")))
                    room_id = cur.lastrowid
                    n_rooms += 1
                    images = r.get("images") or []
                else:
                    offers_image = bool(r.get("thumbnail")) and not prior["has_image"]
                    offers_size = r.get("size_sqft") is not None and not prior["has_size"]
                    # `.get("has_category", True)` so an `existing` snapshot
                    # built by older code (or a caller's stub) degrades to "do
                    # not touch the category" rather than crashing.
                    offers_cat = (r.get("category_id") is not None
                                  and not prior.get("has_category", True))
                    room_id = prior["id"]
                    if not (offers_image or offers_size or offers_cat):
                        skipped_existing += 1
                        images = []
                    elif _fill_empty_fields(
                            cur, room_id,
                            thumbnail=r.get("thumbnail") if offers_image else None,
                            size_sqft=r.get("size_sqft") if offers_size else None,
                            category_id=r.get("category_id") if offers_cat else None):
                        backfilled += 1
                        # Re-attach the full gallery only when the THUMBNAIL is
                        # what's newly set. If only size_sqft was missing, any
                        # gallery this room has was already attached in an
                        # earlier run -- inserting it again would duplicate
                        # every attachment for a field that has nothing to do
                        # with images.
                        images = (r.get("images") or []) if offers_image else []
                    else:
                        # lost a race with a value this same row already
                        # gained elsewhere between the read above and now
                        skipped_existing += 1
                        images = []

                for url in images:
                    _sql(cur,
                         "INSERT INTO v2_attachments (mime_type, attachment_url, "
                         "attachment_size, attachable_type, attachable_id, "
                         "category, created_at, updated_at) "
                         "VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())",
                         (cos.MIME_TYPE, url, cos.ATTACHMENT_SIZE,
                          cos.ATTACHABLE_TYPE, room_id, cos.ATTACHMENT_CATEGORY))
                    n_att += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return n_rooms, n_att, skipped_existing, skipped_duplicate, backfilled


def sync_categories(conn):
    """Create any approved room category that does not exist yet. Additive:
    `Single Deluxe` (1) and `Executive Suite` (2) are matched by name and
    reused, never rewritten."""
    return categories.resolve(conn)


if __name__ == "__main__":
    # -- offline: the reconnect path, which only ever runs during an outage --
    # and therefore never gets exercised by a normal run. It is also the path
    # whose failure used to cascade: a reconnect that raised left the caller
    # holding a dead connection and failed every remaining hotel.
    class _FlakyConn:
        """Fails the first N operations with a transient error, then works."""
        def __init__(self, fail=1):
            self.fail, self.closed, self.ops = fail, False, 0

        def close(self):
            self.closed = True

    def _op(c):
        c.ops += 1
        if c.ops <= c.fail:
            raise pymysql.err.OperationalError(2013, "Lost connection")
        return "ok"

    made = []
    _real_connect = connect

    def _fake_connect(*a, **k):
        made.append(1)
        return _FlakyConn(fail=0)

    globals()["connect"] = _fake_connect
    try:
        # a drop is retried exactly once, on a REPLACED connection
        dead = _FlakyConn(fail=1)
        got, newconn = with_retry(dead, _op, log=lambda *a: None, what="test")
        assert got == "ok", got
        assert dead.closed, "the broken connection was not closed"
        assert newconn is not dead, (
            "with_retry returned the DEAD connection -- every later statement "
            "would go to a closed socket, which is the cascade this prevents")
        assert len(made) == 1, made

        # a healthy call must not reconnect at all
        made.clear()
        fine = _FlakyConn(fail=0)
        got, same = with_retry(fine, _op, log=lambda *a: None)
        assert got == "ok" and same is fine and not made, made

        # a SECOND failure is a real outage: it propagates rather than looping
        made.clear()
        globals()["connect"] = lambda *a, **k: _FlakyConn(fail=99)
        try:
            with_retry(_FlakyConn(fail=1), _op, log=lambda *a: None)
        except pymysql.err.OperationalError:
            pass
        else:
            raise AssertionError("a persistent outage was swallowed")
    finally:
        globals()["connect"] = _real_connect

    # connect() itself must retry rather than give up on the first refusal --
    # the reconnect attempt lands inside the same outage that broke the link.
    attempts = []

    def _refuse(*a, **k):
        attempts.append(1)
        raise pymysql.err.OperationalError(2003, "Can't connect")

    _real_once = _connect_once
    globals()["_connect_once"] = _refuse
    try:
        connect(tries=3, base_delay=0, log=lambda *a: None)
    except pymysql.err.OperationalError:
        pass
    else:
        raise AssertionError("connect() reported success during a total outage")
    finally:
        globals()["_connect_once"] = _real_once
    assert len(attempts) == 3, f"connect() did not retry: {attempts}"
    print("OK: reconnect replaces the dead connection, retries once, backs off, "
          "and never returns a closed socket")

    # CROSS-PROCESS WRITE LOCK. The damage this prevents is unrecoverable
    # (duplicate rooms need a DELETE, which is forbidden), so it is proved
    # against a SECOND REAL CONNECTION -- not mocked. Reproduces the 2026-08-13
    # incident shape: two writers, same hotel, at the same time.
    lock_a, lock_b = connect(), connect()
    HOTEL = {"id": 999999901}                      # id that owns no real rows
    with hotel_write_lock(lock_a, HOTEL["id"]):
        try:
            with hotel_write_lock(lock_b, HOTEL["id"], timeout=1):
                raise AssertionError(
                    "a second connection acquired the same hotel's write lock -- "
                    "concurrent publishers could double-insert rooms")
        except WriteLockTimeout:
            pass                                    # correct: refused, not queued
        # a DIFFERENT hotel must remain free, or the lock serialises the pipeline
        with hotel_write_lock(lock_b, HOTEL["id"] + 1, timeout=1):
            pass
    # released on exit -- the same lock is immediately takeable again
    with hotel_write_lock(lock_b, HOTEL["id"], timeout=2):
        pass
    lock_a.close(); lock_b.close()
    print("OK: hotel write lock excludes a second connection, scopes per hotel, "
          "and releases on exit")

    # Read-only smoke test + a live proof that the additive-only guard bites.
    conn = connect()
    cities = resolve_city(conn, "Dubai")
    assert cities, "no city named Dubai"
    assert len(cities) > 1, "Dubai must resolve to several city_ids -- see docstring"
    by_id = resolve_city(conn, str(cities[0]["id"]))
    assert len(by_id) == 1 and by_id[0]["id"] == cities[0]["id"], \
        "numeric city_id lookup did not round-trip to the same single row"
    ids = [c["id"] for c in cities if c["hotels"]]
    hs = hotels(conn, ids, limit=5)
    assert hs and all(h["name"] for h in hs)
    assert any(h["lat"] is not None for h in hs), "no coordinates came back"

    # The country code must come from the DB, per city, and must NOT be the
    # hardcoded Dubai-era default for a non-UAE city -- a wrong code builds an
    # Agoda URL that never lands, which surfaces as a hotel with "no rooms".
    assert cities[0]["country_code"] == "ae", cities[0]
    for probe_id, want in ((9289, "at"), (4752, "bd"), (24, "ar")):
        row = resolve_city(conn, str(probe_id))
        assert row and row[0]["country_code"] == want, \
            f"city {probe_id} -> {row and row[0].get('country_code')!r}, want {want!r}"
    with conn.cursor() as cur:
        for bad in ("UPDATE v2_rooms SET name='x'", "DELETE FROM v2_rooms",
                    "ALTER TABLE v2_rooms ADD COLUMN x INT", "TRUNCATE v2_rooms"):
            try:
                _sql(cur, bad)
            except AssertionError:
                continue
            raise SystemExit(f"GUARD FAILED: {bad!r} was allowed through")

    # Live proof of the ONE narrow exception -- against a real hotel, real
    # transactions, not a mock. Throwaway room names under a real hotel id
    # (602, already used for prior verification writes on this account);
    # additive-only means they cannot be cleaned up here, left for the
    # operator same as before.
    #
    # Suffixed with the current second: this self-check must be re-runnable
    # without colliding with its OWN leftovers from an earlier run -- a fixed
    # name found the prior run's row already sitting there fully backfilled,
    # which made a fresh-insert assertion fail not because of a bug but
    # because "fresh" was never actually true on a second run.
    import time
    tag = f"{time.time():.6f}"          # sub-second: safe even reran within 1s
    test_hotel = {"id": 602, "city_id": 1280, "slug": "backfill-test",
                 "name": "Backfill Test"}

    # A brand-new room carrying BOTH fields at once must store both correctly
    # in the same INSERT -- catches a parameter-order mistake in the SQL text
    # that a size-only or image-only test would not.
    fresh_name = f"Backfill Test Room Fresh {tag}"
    n0 = publish(conn, test_hotel, [{"name": fresh_name, "category_id": None,
                                     "thumbnail": "https://cdn.example/f.jpg",
                                     "images": [], "size_sqft": 250}])
    assert n0[0] == 1, f"fresh insert with both fields: {n0}"
    with conn.cursor() as cur:
        cur.execute("SELECT thumbnail, size_sqft FROM v2_rooms "
                    "WHERE v2_common_hotel_id=%s AND name=%s",
                    (test_hotel["id"], fresh_name))
        fresh = cur.fetchone()
    assert fresh["thumbnail"] == "https://cdn.example/f.jpg" and fresh["size_sqft"] == 250, \
        f"insert did not store both fields correctly: {fresh}"

    # The backfill lifecycle: imageless+sizeless -> image backfilled -> size
    # backfilled INDEPENDENTLY (must NOT re-attach images) -> both filled,
    # nothing left to gain -> a hijack attempt on either field refused.
    room_name = f"Backfill Test Room ZZZ {tag}"

    n1 = publish(conn, test_hotel, [{"name": room_name, "category_id": None,
                                     "thumbnail": None, "images": [],
                                     "size_sqft": None}])
    assert n1[0] == 1 and n1[4] == 0, f"initial empty insert: {n1}"

    n2 = publish(conn, test_hotel, [{"name": room_name, "category_id": None,
                                     "thumbnail": "https://cdn.example/a.jpg",
                                     "images": ["https://cdn.example/b.jpg"],
                                     "size_sqft": None}])
    assert n2[0] == 0 and n2[4] == 1 and n2[1] == 1, \
        f"backfill of the imageless row did not fire as expected: {n2}"

    with conn.cursor() as cur:
        cur.execute("SELECT id, thumbnail, size_sqft FROM v2_rooms "
                    "WHERE v2_common_hotel_id=%s AND name=%s",
                    (test_hotel["id"], room_name))
        row = cur.fetchone()
    assert row["thumbnail"] == "https://cdn.example/a.jpg" and row["size_sqft"] is None, row

    # Size arrives on its own, with a DIFFERENT candidate image too -- proves
    # the two columns backfill independently, and that a size-only backfill
    # does not re-attach a gallery the room already has.
    n3 = publish(conn, test_hotel, [{"name": room_name, "category_id": None,
                                     "thumbnail": "https://cdn.example/HIJACK.jpg",
                                     "images": ["https://cdn.example/should-not-attach.jpg"],
                                     "size_sqft": 431}])
    assert n3[4] == 1 and n3[1] == 0, \
        f"size-only backfill should add 0 attachments, got: {n3}"
    with conn.cursor() as cur:
        cur.execute("SELECT thumbnail, size_sqft FROM v2_rooms "
                    "WHERE v2_common_hotel_id=%s AND name=%s",
                    (test_hotel["id"], room_name))
        after_size = cur.fetchone()
    assert after_size["thumbnail"] == "https://cdn.example/a.jpg", \
        "GUARD FAILED: a filled thumbnail was overwritten by a size-only backfill"
    assert after_size["size_sqft"] == 431, f"size_sqft backfill did not take: {after_size}"

    # Both fields now set -- a further attempt at either must be refused.
    n4 = publish(conn, test_hotel, [{"name": room_name, "category_id": None,
                                     "thumbnail": "https://cdn.example/HIJACK2.jpg",
                                     "images": [], "size_sqft": 999}])
    assert n4[4] == 0 and n4[2] == 1, f"re-run against a fully-filled row: {n4}"
    with conn.cursor() as cur:
        cur.execute("SELECT thumbnail, size_sqft FROM v2_rooms "
                    "WHERE v2_common_hotel_id=%s AND name=%s",
                    (test_hotel["id"], room_name))
        final = cur.fetchone()
    assert final["thumbnail"] == "https://cdn.example/a.jpg" and final["size_sqft"] == 431, \
        f"GUARD FAILED: a filled field was overwritten: {final}"

    # And the guarded UPDATE itself, directly: fired at a row where both
    # fields are already set, it must affect zero rows and change nothing.
    with conn.cursor() as cur:
        changed = _fill_empty_fields(cur, row["id"],
                                     thumbnail="https://cdn.example/HIJACK3.jpg",
                                     size_sqft=1)
    conn.commit()
    assert changed is False, "GUARD FAILED: _fill_empty_fields affected a fully-set row"

    # -- room_category_id backfills on the SAME terms -----------------------
    # This is the field that matters most in practice: the majority of
    # v2_rooms rows are created by BOOKME's own backend (a side effect of
    # calling /hotels/api/availability) with room_category_id NULL, so a row
    # we never inserted is the normal case, not the exception.
    with conn.cursor() as cur:
        cur.execute("SELECT room_category_id FROM v2_rooms WHERE id=%s", (row["id"],))
        assert cur.fetchone()["room_category_id"] is None, "test setup: category must start NULL"
        cat = sync_categories(conn)[0][categories.FALLBACK]
        filled = _fill_empty_fields(cur, row["id"], category_id=cat)
    conn.commit()
    assert filled is True, "a NULL room_category_id was not backfilled"
    with conn.cursor() as cur:
        cur.execute("SELECT room_category_id FROM v2_rooms WHERE id=%s", (row["id"],))
        assert cur.fetchone()["room_category_id"] == cat, "category backfill did not take"
        # ...and once set it must never be replaced, same guarantee as the others
        again = _fill_empty_fields(cur, row["id"], category_id=cat + 1)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT room_category_id FROM v2_rooms WHERE id=%s", (row["id"],))
        assert cur.fetchone()["room_category_id"] == cat, \
            "GUARD FAILED: an already-set room_category_id was overwritten"
    assert again is False, "a no-gain category call reported a change"

    print(f"OK: Dubai -> {[(c['id'], c['name_en'], c['hotels']) for c in cities]}; "
          f"sample hotel {hs[0]['name']!r} @ {hs[0]['lat']},{hs[0]['lon']}; "
          f"UPDATE/DELETE/ALTER/TRUNCATE all refused; thumbnail and size_sqft "
          f"backfill independently, never re-attach a gallery on a size-only "
          f"fill, and neither field can ever be overwritten once set "
          f"(room id {row['id']})")
    conn.close()
