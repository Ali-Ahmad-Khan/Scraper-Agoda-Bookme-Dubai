# Room imagery pipeline — Bookme × Agoda

Bookme sells wholesale hotel inventory and, for most rooms, shows a **hotel**
photo instead of a photo of the room being sold. Bookme's own API admits it
per room, with `AccurateMedia: false`.

This pipeline fixes that. Name a city; it reads that city's hotels out of
Bookme's own database, finds each hotel's real rooms and photographs on Agoda,
downloads the actual image bytes, uploads them to Bookme's Tencent COS bucket,
and writes `v2_rooms` + `v2_attachments` rows so the live site renders the
right picture against the right room.

This document is the **operating manual** — how to run it, what it writes,
what to do when something goes wrong. For *how the matching decides what goes
where* (the algorithm, its vetoes, the measured evidence behind every
threshold), see [`ARCHITECTURE.md`](ARCHITECTURE.md).

## Run it

```bash
source venv/bin/activate
python -m pipeline.run
```

With no `--city`, this drops into an **interactive wizard**: enter a city name
or numeric id, see every matching `city_id` with hotel counts and how many are
runnable right now vs. already published (against `LEDGER_STALE_DAYS`), then
choose whole-city or a bound. `b` steps back one question at any point, `q`
quits. This is the path for a human at a terminal.

For automation (cron, a scheduled job), skip the wizard with flags:

```bash
python -m pipeline.run --city Dubai --limit 20
```

| flag | meaning |
|---|---|
| `--city NAME` | a name or numeric `city_id`, resolved against the `cities` table. Omit to enter the wizard instead |
| `--city-id N` | (needs `--city`) repeatable; skip the confirmation prompt and use these ids |
| `--yes` | (needs `--city`) accept every city the name matched |
| `--limit N` | (needs `--city`) process at most N hotels — **always start here for a city you haven't run before** |
| `--random` | shuffle before applying `--limit`, for a representative sample instead of the lowest hotel ids |
| `--rooms-from agoda\|both` | (needs `--city`) `both` (default): the real pipeline. `agoda`: a deliberately smaller, faster pass — see below |
| `--dry-run` | do everything except upload images and write to MySQL. Does not take or need the run lock |
| `--no-booking` | skip the Booking.com gap-fill (on by default) — see below |
| `--selftest` | offline checks, no network, no database |

### Booking.com — second source, and the fallback when Agoda fails

Agoda is tried first for every hotel. Booking.com then covers what Agoda did
not, in two distinct ways:

**Agoda is the first layer and stays the primary source.** It is tried for every
hotel, its full escalation ladder runs, and its images always win — Booking is
only ever offered a room that has **no photograph after Agoda has finished**.

| Agoda result | what Booking does |
|---|---|
| matched, imaged every room | **nothing** — the hotel is not even looked up |
| matched, imaged some rooms | fills the remainder |
| matched, but Bookme sells far more rooms than Agoda showed | fills every Bookme room Agoda missed — those are already imageless, so no extra trigger is needed |
| matched, rendered no rooms | fills every Bookme room |
| could not match the hotel at all | supplies imagery for the Bookme rooms, which would otherwise publish nothing (D-37) |
| unreachable (blocked/503) | same, and the Agoda fetch is skipped so a refusing host is not hammered |

In every row Booking supplies **imagery for a room Bookme already sells**. It
never introduces a room type, and never replaces an Agoda match (D-34).

Booking is the only source examined that models "this photo belongs to this
room" as a first-class fact (`associated_rooms`), which is why it is trusted for
this at all.

Four properties matter operationally:

- **It only ever adds.** It never replaces an Agoda match, and it never
  publishes a Booking room type that Bookme does not sell (D-34).
- **It costs nothing where Agoda already worked.** A hotel with no gaps is
  never even looked up. Hotels *with* gaps cost ~11 s each.
- **It needs no credential and no setup.** The AWS WAF token is minted by the
  pipeline in headless Chromium and re-minted automatically on expiry (D-33).
- **A hotel it cannot geo-verify still publishes**, with named, categorised but
  imageless rooms — strictly better than the hotel being skipped, which is what
  used to happen.

Every hotel it was asked about lands in `booking_fill.csv` with the outcome,
including the ones it could not help — `no gaps`, `no geo-verified candidate`,
`no photographed rooms on any of N date shapes`, and `none matched` are four
different facts, deliberately not flattened into one zero.

Turn it off with `--no-booking` to reproduce Agoda-only behaviour exactly.

### If Agoda stops answering — the circuit breaker

Agoda blocks bursts, and a block can outlive a run. Left alone, that produced a
**livelock**: six failures, a 420 s cooldown, six more failures, forever — every
hotel logged as "no agoda match" while the run looked healthy. An unattended
city run could burn hours that way. What happens now:

| consecutive hotels Agoda cannot be asked about | behaviour |
|---|---|
| each cooldown | stand-down **doubles** — 420 s, 840 s, 1680 s … capped at 3600 s. Resets on the first success |
| 3 | loud banner + the Agoda session is **rotated** (fresh cookies/identity) |
| 8 | the run **stops**, folder labelled `-AGODA-DOWN`, `stopped_by_agoda_breaker: true` in the manifest |

Two things this deliberately does *not* do: it does not record an outage as
`no_agoda_match` (that is a separate `agoda_unreachable` reason and its own
count), and it does not stop the run from producing output — those hotels go to
Booking instead, so a run during an Agoda block still publishes.

Everything committed before the stop is safe. Re-run the same city later; the
ledger resumes where it left off.

#### QA'ing it without a database write

```bash
python -m pipeline.run --city Dubai --limit 50 --dry-run --yes
```

Then open `booking_fill.csv` in the run folder and spot-check the rows where
`filled > 0`. Each carries the resolved `outcome` slug and the exact
`rooms_filled` names, so a reviewer can open
`https://www.booking.com/hotel/ae/<slug>.html` and confirm two things:

1. **Right building** — the page's *title*, not its slug (see the trap in
   `HANDOVER.md` §3.6).
2. **Right rooms** — the named rooms exist on that page and are the same room
   type Bookme sells under that name.

A wrong building should be impossible (both gates must pass) — if you find one,
that is a real defect and the candidate's name score and distance belong in the
report. A room-name mismatch on an *apartment-style* property is the known,
accepted risk in D-30, not a new bug.

To compare against the baseline, run the same command with `--no-booking` and
diff `rooms_with_candidate_images` / `rooms_without_candidate_images` in the two
`manifest.json` files — those two counts are computed from the candidate rows
themselves, so unlike `rooms_published_with_no_image` they are meaningful in a
dry run.

### `--rooms-from`: `both` is the default, even for a small `--limit`

The database supplies hotel identity for free, but holds **no room names**
(`v2_rooms` started this project at 11 rows for 89,015 hotels) — those come
from a live source. Agoda's room grid is addressable directly by `agoda_id`:
one call, no search. Bookme's own supplier names come from its **partner
API**, `POST /hotels/api/availability`, keyed by the hotel's `slug` **alone**
— no search, no polling, no per-search or per-itinerary ref id to mint first.
The database's slug *is* the live slug (measured 68/68 exact), so a room
lookup is one call per hotel per probe shape, at roughly 0.3s each with 8
concurrent workers (~3.8 hotels/s, zero throttling). This replaced an earlier
design that ran a whole city-wide polling search purely to mint two throwaway
ref ids per hotel — see [`AVAILABILITY_API_PROPOSAL.md`](AVAILABILITY_API_PROPOSAL.md)
for the full measurement behind the switch (+25% more rooms, 2.7× faster on a
5-hotel A/B, and a targeted re-run of a handful of stragglers dropped from
~396s to ~6s, because there is no more fixed city-wide charge to pay just to
reach them).

Cost now scales with **hotel count**, so `--limit` already bounds it — there
is no longer a fixed per-city charge that makes a small run pay full price.
`both` stays the default anyway, for a different reason: `agoda` is a
genuinely smaller pipeline. No Bookme room names means
`rooms_review.csv`/`rooms_unmatched.csv` can never populate (there is nothing
to compare a name against), and every room publishes under Agoda's own
naming instead of Bookme's supplier naming. A "quick test run" defaulted into
that mode would silently test a different, lesser code path than production
runs. Use `agoda` only when Bookme partner credentials (`BOOKME_USERNAME`/
`BOOKME_PASSWORD`) aren't available, or you deliberately want Agoda-only
naming and know that's the trade. The endpoint is also heavily
nondeterministic (six identical calls to one hotel returned between 18 and 44
rooms) — every probe shape is unioned, never trust-first-answer, the same
rule Agoda's own escalation ladder already follows.

A city name is not a city id. `Dubai` resolves to **1280** (1,340 hotels) *and*
**9658** "Bur Dubai" (55 hotels, including Hyatt Regency Dubai Creek Heights),
so every match prints with its hotel count before anything runs. `cities` also
holds a row named "Brazil" carrying 6,732 hotels — a country stored as a city.
Neither is safe to guess at.

## What it writes

Nothing existing is ever modified or deleted, with one narrow, named exception
(below). `pipeline/db.py` refuses any statement that is not a `SELECT`,
`INSERT` or `SHOW`, **at runtime** — so no code path there can `UPDATE`,
`DELETE`, `ALTER` or `TRUNCATE`, including by accident in a future edit. Live
self-test: `python -m pipeline.db` fires all four at the guard and confirms
every one is refused before it reaches the server.

| table | rows added |
|---|---|
| `v2_room_categories` | the approved 25-category taxonomy, created once, existing rows (`Single Deluxe`, `Executive Suite`) reused by name |
| `v2_rooms` | one per room — `hotel_id=0` (matches the table's existing convention), `v2_common_hotel_id` set, `thumbnail` = a COS URL or `NULL`, `size_sqft` = an integer or `NULL` |
| `v2_attachments` | one per remaining image, morphed to `App\Models\Hotels\Room`, category `room-image` |

Images are **hosted, not hotlinked**: every picture is downloaded and
re-uploaded to `sky/assets/images/hotels/rooms/<md5-of-the-bytes>.jpg`. Keying
on the bytes makes upload idempotent — the same picture always lands on the
same key, so a re-run overwrites nothing and an object orphaned by a crash is
silently reused, never leaked.

**A room already carrying an image never re-enters the download/upload path
at all.** This used to run unconditionally for every candidate room, and only
check afterward — inside `db.publish()` — whether the room was already
complete; a re-run of two mostly-finished hotels measured **577 images
mirrored to actually backfill 2 rooms**, with 112 already-complete rooms
mirrored for zero benefit right alongside them. The DB's own "does this room
have an image" fact (the same query `db.publish()` was always going to run)
is now checked *before* mirroring, not after — same re-run shape measured
**46 images mirrored to backfill 8 rooms** afterward. Full writeup:
[`WASTED_WORK_AUDIT.md`](WASTED_WORK_AUDIT.md).

### `size_sqft` — room dimensions

Agoda's room-grid payload carries a size feature per room
(`features[].title`, e.g. `"Room size: 40 m²/431 ft²"`) — confirmed live
across several structurally different properties, and it genuinely varies by
room type on the same hotel (30–80 m² across one 6-room aparthotel, scaling
with bedroom count), not a hotel-level constant that happens to repeat.
Stored as **square feet only** — one column, an integer — by explicit
decision: square metres can be derived from it in code whenever something
needs it, rather than carrying a second, purely-derived column. `NULL` when
Agoda has no size feature for that specific room, or when the room has no
Agoda counterpart at all (a Bookme-only unmatched room). Two Agoda "master
room" entries can share an identical display name and differ in whether they
carry a size at all — this is normal, not a bug (see `ARCHITECTURE.md`).

### The one exception to additive-only: backfilling a missing field

A room can be published with **no image**, **no `size_sqft`**, or both — no
Agoda candidate existed yet, every candidate image failed to download, or
Agoda's size feature was simply absent on the match that won. `db.py`'s one
UPDATE, `_fill_empty_fields()`, fills whichever of the two is still empty, and
only that. The safety property is structural, not a convention to remember:
`SET thumbnail=COALESCE(thumbnail, %s), size_sqft=COALESCE(size_sqft, %s)`
means each column is independently protected — `COALESCE(existing, anything)`
is always just `existing`, so the function cannot be called in a way that
overwrites either field once it is set. A room's name, category, and any
value already in either column, are never touched; `id` is never reassigned.
Proven live in `pipeline/db.py`'s own self-check against the real database:
insert empty → image backfills → a **different** image is refused → size
backfills independently, without re-attaching a gallery the room already has
→ both set → any further attempt at either is refused, including a direct
call to the guarded UPDATE itself.

**Only a missing image schedules an early revisit**, not a missing
`size_sqft` on its own. Re-including a hotel before `LEDGER_STALE_DAYS`
re-runs the full match/escalation cycle (Agoda suggest, geo-verify, the
retry ladder, potentially the Bookme search) — real cost, not worth paying
just to chase a secondary display field. `size_sqft` still backfills for free
whenever a hotel *is* revisited for any other reason (the image gap, or the
365-day cycle) — the write is free; only the scheduling trigger is
image-only. A hotel with an imageless room is flagged in
`ledger_unresolved.csv` (reason `needs_image_backfill`) even though it also
appears in `ledger_published.csv` — both are true at once, and it's what
pulls the hotel back into the next run of its city ahead of the 365-day
window, instead of waiting out the full period for a gap already known about.

## Concurrent-run safety

Two runs touching the same hotel at once is the one failure mode this design
cannot absorb: `v2_rooms` has **no unique constraint** (only `PRIMARY` on
`id`), so the dedupe that stops a re-run duplicating a room is a plain
read-then-insert — atomic against itself inside one transaction, not against
a second process doing the same thing. And it would be **permanent** damage,
because cleaning it up needs a `DELETE`, which this pipeline may never issue.

`acquire_run_lock()` takes an advisory `flock` on `out/.run.lock` before any
write path, held for the process lifetime, released by the OS on any exit
including a crash or `kill -9` — so a dead run never leaves a stale lock
needing manual clearing. A second run started while one is in progress is
refused immediately with a clear message, not silently queued. `--dry-run`
neither takes nor is blocked by the lock, since it writes nothing. Verified
live with a genuine two-process test: run A holds → run B is refused → run A
releases → run C acquires cleanly.

## Crash safety

The unit of atomicity is **one hotel**: its rooms and their attachments
commit together, and only then is the hotel written to the local ledger CSVs
(below — local because Bookme's schema is off-limits for changes).

This section is a summary. **[`RELIABILITY.md`](RELIABILITY.md) is the full
account** — every failure class (internet drops and flaps, the server itself
shutting down, the machine crashing outright, MySQL and COS dropping mid-call),
what handles each one, why, and the runnable test proving it, including a
real `SIGTERM` sent to a live running process and a real resumed harvest read
back from its checkpoint.

- **Ctrl-C** (SIGINT) or a **shutdown signal** (SIGTERM — what `systemctl
  stop`/`docker stop`/a cloud instance shutdown actually send, and previously
  unhandled) → the hotel in flight finishes and commits, the in-progress
  Bookme probe checkpoint is written, then the process exits cleanly
  (releasing the run lock). A second signal of either kind aborts immediately.
- **A machine reboot mid-harvest resumes, it does not restart.** Every
  completed (hotel × probe-shape) call is check-pointed to disk after that
  shape finishes; a fresh process loads it and skips straight to the next
  shape. Verified live: a run was SIGTERM'd after 7 of 12 base shapes (343
  room names held), and the next invocation printed `resuming from
  checkpoint: 7 shape(s) already done` and continued from shape 8. The
  checkpoint is scoped to the exact hotel set **and** probe ladder it was
  earned against, so it can never be loaded by a different city's run and
  silently skip shapes that were never actually asked.
- A crash between commit and ledger write → the next run re-does that hotel;
  the DB-level dedupe on `(v2_common_hotel_id, name)` stops it duplicating
  rooms. The failure mode is repeated work, never bad data. The ledger write
  itself now `fsync`s — durable the moment it returns, not merely handed to
  the OS page cache — and tolerates a torn trailing line from a hard kill
  without becoming unreadable (an unreadable ledger used to mean every hotel
  reads as unpublished and the next run silently re-does the entire city).
- **A dropped MySQL connection** (confirmed live: a real `Connection reset by
  peer` hit during a production run) retries with exponential backoff (up to
  ~2 minutes total) rather than a single attempt — the single-attempt version
  had a real cascade bug: if the one reconnect try itself failed (likely,
  since it lands inside the same outage that broke the original connection),
  the run's connection variable was left bound to the dead socket and **every
  remaining hotel in the run** failed against it. Every read that touches
  MySQL is covered now, not just the write — an unretried drop during the
  initial hotel fetch used to kill the run before a single hotel was attempted.
- COS uploads happen *outside* the transaction, and now retry transport
  faults independently on both the download and the upload leg — a dropped
  packet used to be indistinguishable from a dead link, silently publishing a
  hotel with no pictures at all. Content-addressing still makes any orphan
  from a crash harmless regardless — the next attempt computes the identical
  key and reuses it.
- A hotel published within `LEDGER_STALE_DAYS` (365 days) is skipped on the
  next run of its city, unless it's flagged `needs_image_backfill` (above).

## Reports

Two kinds of output, kept deliberately separate: a **per-run report** (this
run, this folder, never touched again) and a **cross-run ledger** (persists,
read by every future run against the same city).

### Per-run report — `out/runs/<timestamp>-city<ids>-<bound>/`

e.g. `20260811-124235-city24-limit6` or `20260806-155850-city1280+9658-unbound`
— date-time first (sorts chronologically), then which city_id(s), then
whether it was bounded (`limitN`) or the whole city (`unbound`). Traceable
from the folder name alone, without opening `manifest.json`.

| file | contents |
|---|---|
| `manifest.json` | run id, city, probes used, all counts, config snapshot |
| `hotels_to_revisit.csv` | every hotel this run couldn't finish: `unresolved_on_bookme`, `no_rooms_any_date`, `no_agoda_match`, `error` |
| `rooms_review.csv` | candidates in the 62–75 band, highest score first — `candidate_images` (full gallery, `\|`-joined) and `candidate_size_sqft` to judge or apply, `agoda_url`, a blank `decision` column for the reviewer |
| `rooms_unmatched.csv` | Bookme rooms with genuinely no plausible Agoda candidate: every candidate was vetoed, or none cleared even the review floor |
| `booking_fill.csv` | one line per hotel the gap-fill was asked about: how many gaps it found, how many it filled, and the `outcome` — the resolved slug on success, or *why* not |

All the CSVs are flat, with the hotel columns repeated per row — that
repetition is the point, it's what makes them sortable and filterable in a
spreadsheet. The genuinely tree-shaped data lives in `manifest.json`.

`rooms_review.csv` and `rooms_unmatched.csv` both open with a `row_id`
column (1, 2, 3…, assigned in write order) — the stable handle for
referring to one specific row ("row 12"), since nothing else on the row is
guaranteed unique within the file. `ledger_published.csv`/
`ledger_unresolved.csv` carry the same idea (see below), numbered
independently per file and continuing across process restarts, not reset.

`rooms_review.csv`/`rooms_unmatched.csv` only populate on `--rooms-from both`
runs — both come from comparing a Bookme room name against Agoda's
candidates, and with no Bookme rooms fetched (`agoda` mode) there is nothing
to compare, so every room publishes straight from Agoda's own naming and both
files stay header-only. Expected, not a failure to populate.

**Contention is not counter-evidence.** `map_rooms()` assigns each Agoda room
to exactly one Bookme room by strongest evidence first (two Bookme names
sometimes both describe one physical room — rate-plan duplicates are common
on Bookme's side). Losing that draw used to fall straight to `unmatched`
regardless of how good the losing room's OWN match was. It no longer does: a
losing room is scored on its own merit against the accept/review bar exactly
like a winner would be — `>= ROOM_ACCEPT` still publishes (with the same
candidate's images), `ROOM_REVIEW`–`ROOM_ACCEPT` still lands in
`rooms_review.csv`. `rooms_unmatched.csv` is reserved for what's actually
unresolved: every candidate vetoed, or nothing cleared the review floor at
all. Treat a populated `rooms_unmatched.csv` as a real diagnostic signal, not
routine noise — if most of it is 90%+ scores with no veto, something's wrong
upstream of this rule, not with the rooms themselves.

### Applying review decisions — `--apply-reviews`

```
python -m pipeline.run --apply-reviews out/runs/<run>/rooms_review.csv
python -m pipeline.run --apply-reviews out/runs/<run>/rooms_review.csv --dry-run
```

A human fills in `decision` (`yes`/`approve`, case-insensitive; anything
else, including blank, is left alone) on the rows they've eyeballed. Rows are
listed highest-score-first, so the easiest, most-obviously-correct decisions
sort to the top of the file — clear the "near-certain" ones fast, spend the
actual judgment on the rest. `candidate_images`/`candidate_size_sqft` were
already captured in the CSV at match time, so applying a decision costs **no
re-match, no Bookme re-probe, no Agoda re-fetch** — only re-hosting the
approved images to COS and the same `db.publish()` every other room in the
pipeline already goes through, reused unchanged. A row that applies
successfully is removed from the file afterward (it's a work queue, not a
log); one that fails (every candidate image dead) is left in place with a
printed reason. Re-running against the same file is safe either way:
unapproved rows are skipped again, and even if a row were somehow re-applied,
it's a no-op — the write is the same COALESCE guard as everything else (see
below).

### Cross-run ledger — `out/ledger_published.csv`, `out/ledger_unresolved.csv`

Append-only logs (one line per event; loading takes the **last** line per
hotel id, so history survives on disk but only the latest verdict counts) —
CSV, not a database, so the operator can open them directly.

- **`ledger_published.csv`** — every hotel that got rooms + images written,
  and when. `fresh_ids()` reads this to skip a hotel republished within
  `LEDGER_STALE_DAYS`.
- **`ledger_unresolved.csv`** — every hotel that ended a run *without* being
  fully published, and why, so a later run of the same city can retarget just
  these instead of re-walking the whole city to rediscover the same
  failures. A hotel that later succeeds gets a `resolved` row appended, which
  is what the next read honours — it does not linger as a false failure.

## What's concurrent, what isn't, and why

Agoda's page/search API is paced globally (`agoda.py`'s `MIN_INTERVAL` —
1.5s between *any* two Agoda requests, process-wide state, not per hotel)
because it blocks bursts: confirmed live, a 465-hotel run issuing calls as
fast as possible got 502s from hotel 194 onward, with the block outliving the
run. That pacing stays exactly as it is — threading hotel processing would
not even speed it up, since the pacer's state is shared regardless of thread
count.

**Image mirroring is a different story.** The source (Agoda's/bstatic's image
CDN) and the destination (Tencent COS) are neither the paced endpoint nor
each other, and neither is rate-limited the way the page API is — so
downloading and re-uploading a hotel's images one at a time was pure wasted
wall-clock time. `mirror_all_images()` fetches all of one hotel's images
concurrently (`IMAGE_WORKERS = 8` in `run.py`, via `ThreadPoolExecutor`). A
hotel with 5 rooms × 6 images went from 30 sequential round trips to up to 8
concurrent ones. Order within each room is preserved regardless of which
thread finishes first (`Executor.map()` returns results in input order, not
completion order) — proven with a real timing assertion in `selftest()`
(6 images complete in ~55ms against a sequential floor of 300ms+), not just
that the code runs without error. A real concurrency bug was caught and fixed
before shipping (a premature "done" marker that could hand back a URL for an
object not yet uploaded) — see `ARCHITECTURE.md`.

`categories.classify()` (the taxonomy) costs 6 microseconds per room — an
entire 1,340-hotel city, ~0.065 seconds total — against a single Agoda call
paced at 1.5 seconds. Not a target for optimisation at any scale this
pipeline runs at.

**Bookme's own probe is concurrent too, and unrelated to Agoda's pacer.**
`/hotels/api/availability` has no rate limit of its own — measured zero
throttling, zero 429s, no cooldown at up to 16 concurrent workers, throughput
flattening past 8 (0.52 hotels/s at 1 worker → 3.76/s at 8 → 4.61/s at 16).
`harvest_rooms()` runs the per-hotel, per-shape probe pass through the same
bounded `ThreadPoolExecutor(8)` bulkhead pattern as `mirror_all_images()`,
entirely independent of Agoda's `MIN_INTERVAL` pacer — the two APIs share no
state, so nothing about Bookme's concurrency can trip Agoda's burst
detection or vice versa.

## Layout

| file | role |
|---|---|
| `pipeline/run.py` | the driver: wizard/flags → scope → harvest → match → publish → report |
| `pipeline/db.py` | MySQL. Additive-only at runtime, plus the one named backfill exception |
| `pipeline/cos.py` | image bytes → Tencent COS → public CDN URL, thread-safe |
| `pipeline/categories.py` | the approved room taxonomy and its classifier |
| `pipeline/ledger.py` | local, append-only CSV record of what has been published and what still needs work |
| `pipeline/match.py` | name normalisation, scoring, and the hard vetoes — see `ARCHITECTURE.md` |
| `pipeline/bookme.py` | Bookme's partner API — token auth, per-hotel `/availability` by slug |
| `pipeline/agoda.py` | Agoda's suggest and room-grid endpoints, including room size extraction |
| `pipeline/agoda_browser.py` | browser fallback, reads the page's own XHR off the wire, not the DOM |
| `pipeline/booking.py` | Booking.com: autonomous WAF-token minting, geo-verified identity, per-room photos |

Every module runs its own checks: `python -m pipeline.<name>`. `db`, `cos`,
`agoda`, `bookme` and `booking` touch the live services; `categories`,
`ledger`, `match` and `run --selftest` are fully offline.

Configuration lives in `pipeline/config.py` (tuning constants, each commented
with the measurement behind it) and `.env` (credentials, mode `600`).

## See also

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — the matching algorithm: identity
  resolution, the veto system, the probe strategy, and the measured evidence
  (threshold validation across 10 countries, recall/precision tradeoffs)
  behind every constant in `config.py`.
- [`RELIABILITY.md`](RELIABILITY.md) — every crash/network/outage failure
  class this pipeline is built to survive, what handles each one, and the
  runnable test or live demonstration proving it.
- [`WASTED_WORK_AUDIT.md`](WASTED_WORK_AUDIT.md) — redundant work found and
  removed (network calls and queries that re-acquired something already
  available), with before/after measurements.
- [`AVAILABILITY_API_PROPOSAL.md`](AVAILABILITY_API_PROPOSAL.md) — the
  measurement record behind moving from a city-wide search to a per-hotel
  slug lookup (see "`--rooms-from`" above).
- [`REPORT.md`](REPORT.md) — the current, presentation-ready technical
  report: the problem, the architecture, the measured results, a production-
  readiness assessment. Written to stand alone for a non-engineer audience.
- [`DB_FINDINGS.md`](DB_FINDINGS.md),
  [`ROOM_CATEGORIES_PROPOSAL.md`](ROOM_CATEGORIES.md),
  [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) — historical decision
  records from earlier in the project. Kept for audit trail; superseded by
  this file and `ARCHITECTURE.md` for current behaviour. Each carries its own
  "superseded" notice at the top.
