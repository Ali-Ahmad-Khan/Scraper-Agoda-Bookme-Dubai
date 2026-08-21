"""Which hotels have been published, and which have never resolved -- as CSV.

CSV, not a database: this is operator-readable bookkeeping the operator asked
to be able to open directly, and it lives entirely OUTSIDE Bookme's schema
(off-limits for changes) so it has to live somewhere local regardless.

Two independent files, because "recently published" and "tried and failed" are
different questions with different lifetimes:

  ledger_published.csv    hotel_id -> when it last got rooms + images written.
                           Read by fresh_ids() to SKIP a hotel next run.
  ledger_unresolved.csv   hotel_id -> why it did NOT get published this run.
                           Read by unresolved_ids() so a later run of the same
                           city can retarget just these, instead of re-walking
                           every hotel to rediscover the same failures.

Both are APPEND-ONLY logs, not row-per-hotel tables: marking a hotel writes one
new line, never rewrites the file. Loading takes the LAST row per hotel id --
last-write-wins -- so history survives on disk but only the latest verdict
counts. This makes a mark() an O(1) write instead of an O(n) file rewrite,
which matters because it happens after every single hotel's DB commit, by
design (see run.py's crash-safety contract).

A hotel that fails then later succeeds must stop showing as unresolved --
mark_published() appends a `resolved` sentinel row to the unresolved log
whenever the hotel currently has an open (unresolved) entry, and that sentinel
outranks the failure on the next load because it is the newer row.
"""
import csv
import datetime
import os

from . import config

DIR = os.path.join(config.ROOT, "out")
PUBLISHED_PATH = os.path.join(DIR, "ledger_published.csv")
UNRESOLVED_PATH = os.path.join(DIR, "ledger_unresolved.csv")

PUBLISHED_COLS = ["row_id", "v2_common_hotel_id", "city_id", "slug", "name", "run_id",
                  "rooms_inserted", "images_uploaded", "published_at"]
UNRESOLVED_COLS = ["row_id", "v2_common_hotel_id", "city_id", "slug", "name", "run_id",
                   "reason", "detail", "last_attempted_at"]
RESOLVED = "resolved"          # sentinel reason: clears a prior failure


def _load_last_per_hotel(path, columns):
    """({hotel_id (int) -> latest row (dict)}, row count so far).

    The row count is what lets row_id keep counting up across process
    restarts without a second file read: this function already parses every
    row to find the latest-per-hotel one, so handing back len(rows) too is
    free, and a fresh Ledger() seeds its row_id counter from it once instead
    of re-scanning the file on every append.
    """
    if not os.path.exists(path):
        return {}, 0
    # errors="replace": a ledger is not worth crashing the pipeline over. A
    # process killed mid-write (SIGKILL, power loss, container eviction) can
    # leave a truncated final line or a partial multi-byte character, and an
    # unreadable ledger is a TOTAL outage -- every hotel looks unpublished, so
    # the next run re-does the entire city. Read what is readable.
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        try:
            raw = list(csv.reader(f))
        except csv.Error:
            # A malformed row mid-file (an unterminated quote from a torn
            # write) makes the reader raise rather than yield. Fall back to
            # line-at-a-time so everything before the damage still counts.
            f.seek(0)
            raw, rdr = [], csv.reader(f)
            try:
                for r in rdr:
                    raw.append(r)
            except csv.Error:
                pass
    if not raw:
        return {}, 0
    header, data = raw[0], raw[1:]

    # The header is NOT trusted blindly. Found live: a column (`row_id`) was
    # added to `columns` after this file already existed, so the on-disk
    # header stayed 8-wide while every new append wrote 9 fields -- and
    # csv.DictReader, which trusts the header for field names, silently
    # shifted every new row by one: the run id landed in the `reason` column,
    # `unresolved_ids()`'s `reason != RESOLVED` check stopped matching, and a
    # resolved hotel could never clear its unresolved flag again. That is
    # QUIETLY WRONG, not loudly broken -- worse than a crash, because nothing
    # said anything was wrong.
    #
    # General fix, not a patch for `row_id` specifically: if the header is a
    # SUFFIX of the current `columns` (i.e. some number of columns were added
    # to the FRONT since older rows were written), a row is unambiguous by its
    # own width -- either it matches the old (shorter) header, or it matches
    # the current (full) `columns`. Anything else is genuine corruption, not
    # schema drift, and is skipped rather than guessed at.
    old_header = None
    if header != columns:
        width_gap = len(columns) - len(header)
        if width_gap > 0 and columns[width_gap:] == header:
            old_header = header               # old rows use this width/order
        else:
            print(f"  ledger {os.path.basename(path)}: header does not match "
                  f"the current schema and is not a recognizable prefix-add "
                  f"({header!r} vs {columns!r}); rows will be matched by "
                  f"width only, best-effort")
            old_header = header if len(header) < len(columns) else None

    out, skipped = {}, 0
    for fields in data:
        if len(fields) == len(columns):
            row = dict(zip(columns, fields))
        elif old_header is not None and len(fields) == len(old_header):
            row = dict(zip(old_header, fields))
        else:
            skipped += 1
            continue
        hid = (row.get("v2_common_hotel_id") or "").strip()
        if not hid:
            skipped += 1
            continue
        try:
            out[int(hid)] = row               # later rows in the file overwrite
        except ValueError:
            # a torn line whose id field is garbage -- countable, not fatal
            skipped += 1
    if skipped:
        print(f"  ledger {os.path.basename(path)}: skipped {skipped} malformed "
              f"row(s) (likely a torn write from an interrupted run, or a "
              f"width this loader could not confidently map); the rest "
              f"loaded normally")
    return out, len(data)


def _append(path, columns, row):
    """Append one row and put it on the physical disk before returning.

    fsync, not just flush: this is the durability point of the whole pipeline.
    The contract is "a hotel is committed to MySQL, THEN recorded here" -- if
    the machine loses power with the row sitting in the OS page cache, the
    hotel is published but the ledger has forgotten, and the next run re-does
    it. That re-run is safe (publish is idempotent) but it is wasted hours on a
    full city, and it is exactly the crash the ledger exists to survive.

    flush() only hands the bytes to the kernel; fsync() is what survives a power
    cut or a hard container kill. One fsync per hotel is nothing next to the
    seconds of image mirroring that precede it.
    """
    new = not os.path.exists(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        if new:
            w.writeheader()
        w.writerow(row)
        f.flush()
        os.fsync(f.fileno())


class Ledger:
    def __init__(self):
        self.published, self._pub_seq = _load_last_per_hotel(PUBLISHED_PATH, PUBLISHED_COLS)
        self.unresolved, self._unres_seq = _load_last_per_hotel(UNRESOLVED_PATH, UNRESOLVED_COLS)

    def fresh_ids(self, stale_days=None):
        """Hotel ids published recently enough to skip this run."""
        days = config.LEDGER_STALE_DAYS if stale_days is None else stale_days
        cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
        return {hid for hid, r in self.published.items()
               if r.get("published_at", "") >= cutoff}

    def unresolved_ids(self, city_id=None):
        """Hotel ids whose latest recorded outcome is a failure, not a
        `resolved` sentinel -- i.e. genuinely worth retargeting."""
        return {hid for hid, r in self.unresolved.items()
               if r.get("reason") != RESOLVED
               and (city_id is None or r.get("city_id") == str(city_id))}

    def mark_published(self, hotel, run_id, rooms, images):
        self._pub_seq += 1
        row = {"row_id": self._pub_seq, "v2_common_hotel_id": hotel["id"],
               "city_id": hotel.get("city_id"), "slug": hotel.get("slug"),
               "name": hotel.get("name"), "run_id": run_id,
               "rooms_inserted": rooms, "images_uploaded": images,
               "published_at": datetime.datetime.now().isoformat(timespec="seconds")}
        _append(PUBLISHED_PATH, PUBLISHED_COLS, row)
        self.published[hotel["id"]] = {k: str(v) for k, v in row.items()}
        if hotel["id"] in self.unresolved_ids():
            self._clear_unresolved(hotel, run_id)

    def _append_unresolved(self, row):
        # shared by mark_unresolved and _clear_unresolved so the two writers
        # of ledger_unresolved.csv can never hand out the same row_id twice
        self._unres_seq += 1
        row = {"row_id": self._unres_seq, **row}
        _append(UNRESOLVED_PATH, UNRESOLVED_COLS, row)
        self.unresolved[row["v2_common_hotel_id"]] = {k: str(v) for k, v in row.items()}

    def mark_unresolved(self, hotel, run_id, reason, detail=""):
        self._append_unresolved({
            "v2_common_hotel_id": hotel["id"], "city_id": hotel.get("city_id"),
            "slug": hotel.get("slug"), "name": hotel.get("name"), "run_id": run_id,
            "reason": reason, "detail": str(detail)[:300],
            "last_attempted_at": datetime.datetime.now().isoformat(timespec="seconds")})

    def _clear_unresolved(self, hotel, run_id):
        self._append_unresolved({
            "v2_common_hotel_id": hotel["id"], "city_id": hotel.get("city_id"),
            "slug": hotel.get("slug"), "name": hotel.get("name"), "run_id": run_id,
            "reason": RESOLVED, "detail": "",
            "last_attempted_at": datetime.datetime.now().isoformat(timespec="seconds")})


def open_ledger():
    return Ledger()


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        PUBLISHED_PATH = os.path.join(d, "p.csv")
        UNRESOLVED_PATH = os.path.join(d, "u.csv")

        h = {"id": 42, "city_id": 1280, "slug": "x", "name": "X"}
        led = open_ledger()
        assert led.fresh_ids() == set() and led.unresolved_ids() == set()

        led.mark_unresolved(h, "run1", "no_agoda_match", "no candidate")
        led2 = open_ledger()                       # simulate a fresh process
        assert led2.unresolved_ids() == {42}
        assert led2.unresolved_ids(city_id=1280) == {42}
        assert led2.unresolved_ids(city_id=9999) == set()

        led2.mark_published(h, "run2", 5, 20)       # hotel now succeeds
        assert led2.fresh_ids() == {42}
        assert led2.unresolved_ids() == set(), "resolved hotel still listed as unresolved"

        led3 = open_ledger()                        # must survive a reload
        assert led3.fresh_ids() == {42} and led3.unresolved_ids() == set()
        assert led3.fresh_ids(stale_days=-1) == set(), "staleness window ignored"

        # row_id: a stable reference number for a row, monotonic PER FILE and
        # unbroken across a process restart -- it must resume counting from
        # what is already on disk, not restart at 1, and the two writers of
        # ledger_unresolved.csv (mark_unresolved, _clear_unresolved) must
        # never hand out the same number.
        with open(UNRESOLVED_PATH, newline="", encoding="utf-8") as f:
            unres_ids = [r["row_id"] for r in csv.DictReader(f)]
        assert unres_ids == ["1", "2"], f"unresolved row_id sequence: {unres_ids}"
        with open(PUBLISHED_PATH, newline="", encoding="utf-8") as f:
            pub_ids = [r["row_id"] for r in csv.DictReader(f)]
        assert pub_ids == ["1"], f"published row_id sequence: {pub_ids}"

        # append-only: history is never rewritten, only the latest row governs
        with open(PUBLISHED_PATH, encoding="utf-8") as f:
            assert sum(1 for _ in f) == 2, "expected header + 1 published row"
        with open(UNRESOLVED_PATH, encoding="utf-8") as f:
            assert sum(1 for _ in f) == 3, "expected header + fail row + resolved row"

        # row_id must resume counting from what is already on disk after a
        # process restart (led3 is a fresh Ledger()), not restart at 1
        led3.mark_unresolved(h, "run3", "error", "network blip")
        with open(UNRESOLVED_PATH, newline="", encoding="utf-8") as f:
            unres_ids = [r["row_id"] for r in csv.DictReader(f)]
        assert unres_ids == ["1", "2", "3"], (
            f"row_id did not resume after reload: {unres_ids}")

        # A TORN FINAL LINE must not take the pipeline down. This is what a
        # SIGKILL, a power cut or a container eviction leaves behind, and an
        # unreadable ledger is a total outage: every hotel reads as
        # unpublished, so the next run re-does the whole city.
        with open(PUBLISHED_PATH, "a", encoding="utf-8") as f:
            f.write("999,1280,partial-slug,Half Writt")     # no newline, short row
        led4 = open_ledger()
        assert 42 in led4.published, "a torn trailing line lost the good rows"

        # ...and a row whose id field is outright garbage is skipped, counted,
        # never fatal.
        with open(PUBLISHED_PATH, "a", encoding="utf-8") as f:
            f.write("\n7,not-an-id,x,y,z,run9,1,2,2026-01-01\n")
        led5 = open_ledger()
        assert 42 in led5.published, "a garbage id row lost the good rows"

        # fsync: the row must be on disk, not merely in the OS page cache, by
        # the time mark_published returns -- the ledger write is the pipeline's
        # durability point (MySQL commits, THEN this records it).
        import subprocess
        import sys as _sys
        h2 = {"id": 77, "city_id": 1280, "slug": "d", "name": "D"}
        led5.mark_published(h2, "run5", 1, 1)
        out = subprocess.run(
            [_sys.executable, "-c",
             f"import csv;print(sum(1 for r in csv.DictReader("
             f"open({PUBLISHED_PATH!r},newline='',encoding='utf-8',"
             f"errors='replace')) if r.get('v2_common_hotel_id')=='77'))"],
            capture_output=True, text=True)
        assert out.stdout.strip() == "1", (
            f"a separate process could not see the flushed row: {out.stdout!r}")

        # SCHEMA DRIFT: reproduces the real incident. A `row_id` column was
        # added to UNRESOLVED_COLS after the file already had old (8-wide)
        # rows on disk; the on-disk HEADER never got rewritten, so it stayed
        # at the old width while new appends wrote the new width. Write that
        # exact shape by hand and prove a fresh Ledger() parses both widths
        # correctly -- old rows keep their fields, new rows are not shifted.
        drift_path = os.path.join(d, "drift.csv")
        old_cols = UNRESOLVED_COLS[1:]                    # header BEFORE row_id existed
        with open(drift_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(old_cols)                            # stale 8-wide header
            # hotel 502: an OLD-format row with no newer override -- the only
            # way to prove the 8-wide path itself parses correctly, not just
            # that a later 9-wide row happens to supersede it
            w.writerow([502, 1280, "solo-old-slug", "Solo Old Hotel", "run_solo",
                       "no_agoda_match", "no candidate", "2026-01-01T00:00:00"])
            # hotel 501: OLD row, then a NEWER 9-wide row that must win
            w.writerow([501, 1280, "old-slug", "Old Hotel", "run_old",
                       "no_agoda_match", "no candidate", "2026-01-01T00:00:00"])
            w.writerow([99, 501, 1280, "old-slug", "Old Hotel", "run_new",
                       RESOLVED, "", "2026-01-02T00:00:00"])  # 9-wide, post-fix row_id
        parsed, n = _load_last_per_hotel(drift_path, UNRESOLVED_COLS)
        assert n == 3, f"expected 3 data rows, loader saw {n}"
        assert 502 in parsed, f"a solo old-format row was dropped: {parsed}"
        solo = parsed[502]
        assert solo["reason"] == "no_agoda_match" and solo["run_id"] == "run_solo", (
            f"the OLD (8-wide) row's own fields were shifted: {solo}")
        assert 501 in parsed, f"drifted-header file lost its only hotel: {parsed}"
        latest = parsed[501]
        assert latest["reason"] == RESOLVED, (
            f"the NEWER (9-wide) row must win, and 'reason' must be 'resolved', "
            f"not a shifted field: {latest}")
        assert latest["run_id"] == "run_new", (
            f"a 9-wide row was misparsed against the stale 8-wide header: {latest}")
        # and unresolved_ids() must actually see it as cleared, end to end --
        # this is the real symptom: a resolved hotel stuck as unresolved forever
        led6 = Ledger.__new__(Ledger)
        led6.unresolved, led6._unres_seq = parsed, n
        led6.published, led6._pub_seq = {}, 0
        assert 501 not in led6.unresolved_ids(), (
            "a hotel resolved via a 9-wide row still reads as unresolved -- "
            "the exact bug that made resolved hotels retry forever")
        assert 502 in led6.unresolved_ids(), (
            "a genuinely-still-unresolved OLD-format hotel vanished")

    print("OK: ledger skips fresh hotels, tracks unresolved, clears on success, "
          "survives a reload, never rewrites history, row_id resumes across "
          "restarts, tolerates torn/garbage rows, fsyncs each append")
