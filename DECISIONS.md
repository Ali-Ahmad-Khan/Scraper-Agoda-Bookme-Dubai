# Decision ledger

Decisions already taken, with the evidence behind them. **Do not re-litigate
these without new evidence.** Each entry says what would justify overriding it —
they are not permanent, they are *settled until the stated condition changes*.

Format: **D-n — decision** · why · what would override it.

---

## Architecture

**D-1 — Bookme rooms come from `POST /hotels/api/availability`, keyed by the
database slug. No city search, no polling, no ref ids.**
The old path ran a city-wide polling search purely to mint two throwaway ref ids
per hotel (~379 s per city, ×3 probes). Measured on 5 hotels: +25% more rooms in
2.7× less time. `v2_common_hotels.slug` **is** the live slug (68/68 exact, 0
mismatches) within a matched environment.
*Override if:* the endpoint is withdrawn (it is undocumented/SSR-only) — the
`single-itinerary` fallback would need reinstating.

**D-2 — `Property.CommonID` is an exact foreign key to `v2_common_hotels.id`
(60/60), so Bookme-side identity is a lookup, not an inference.**
This **overturns `DB_FINDINGS.md` Finding 9** ("CommonID does NOT join", 0/40),
which was measured cross-environment (prod API vs UAT ids).
*Override if:* moving environments — re-verify the join first.

**D-3 — The database and the Bookme API must always be the same environment.**
Prod appends an arbitrary `-<digits>` slug suffix (`hilton-dubai-the-walk-864`);
UAT does not. Crossing them silently resolves ~17% of hotels and reports the
rest as unavailable, with **no error anywhere**. This exact mistake was made
once already and produced a whole invalid analysis.
*Override if:* never. Move `BOOKME_API_BASE` and the MySQL credentials together.

**D-3a — Prod and UAT are structurally identical. Switching is credentials +
domain, nothing else.** Verified live 2026-08-13, same partner credentials:

```
api.bookmesky.com      auth 201 · 'hilton-dubai-the-walk-864' -> 17 rooms, CommonID 2029037
uat-api.bookmesky.com  auth 201 · 'hilton-dubai-the-walk'     -> 17 rooms, CommonID 605
```

Same endpoints, same payload shape, same room count — only the slug namespace
and the `CommonID` id space differ (which is exactly what D-3 governs).
**Corollary:** running against UAT is legitimate and its yield numbers are the
correct answer *for UAT*. An earlier recommendation to "not tune against UAT"
was wrong and is withdrawn — the pipeline's job is to extract maximally from
whichever environment it is pointed at.

**D-4 — Agoda rooms come from `/api/cronos/property/BelowFoldParams/GetSecondaryData`.**
Verified 2026-08-13: parameter sweep (`adults`, `los`, `price_view`,
`all=false`, far dates) changes nothing; omitting dates returns 0. Extraction
quality measured at 100% image coverage and 94.3% `size_sqft`. `supplierCount=0`
was verified truthful against Agoda's own rendered page ("Sold out!").
*Override if:* a primary/pre-fold endpoint is found that returns rooms for
properties this one reports sold out.

---

## Probing

**D-5 — Never trust a single API call. Union across probe shapes.**
Bookme `/availability` is heavily nondeterministic: six identical calls to one
slug returned 19/18/27/38/36/44 rooms. Union across 4 probes found **+46% more
rooms** than the best single probe.
*Override if:* never, while the endpoint remains nondeterministic.

**D-6 — Agoda is deterministic; repeat identical calls buy nothing.**
Measured: 4 identical calls to three properties returned 10/10/10/10, 16×4,
12×4. Unlike Bookme, repetition is pure waste — widen *shapes*, not repeats.

**D-6a — Agoda's escalation ladder ALWAYS runs, even when the base date already
returned rooms. Two plausible shortcuts were tried and rejected (2026-08-13).**

*Rejected (a): "escalate only when the base date returns zero."* Unioning Agoda
across 10 dates over 40 weeks adds nothing over the **best** single date
(Hilton 18/18, Jumeirah 14/14, Dusit 12/12, Movenpick 9/9). That result invites
the shortcut — but the **base date is routinely not the best date**:

```
Hilton Dubai The Walk   base 14 -> ladder 18   (+4, +29%)
Dusit Thani Dubai       base  9 -> ladder 12   (+3, +33%)
Jumeirah Beach Hotel    base 13 -> ladder 14   (+1)
```

The ladder is not searching for *more than* the best date — it is how we **find**
the best date without knowing which one it is. Gating on zero silently drops
those rooms. *(This shortcut was actually implemented and then reverted after
measurement; the union result alone is genuinely misleading.)*

*Rejected (b): "stop once the count stops growing."* The paying rung differed per
hotel (weekend+2, +3, +8), so any early exit is a coin flip on which hotel it robs.

**Known cost:** the ladder is ~90% of Agoda wall-clock (1.7 s base vs 16.7 s with
ladder, per hotel). That is a throughput problem for the pacing/concurrency
layer — **never** solve it by asking Agoda fewer questions.

**D-6b — Agoda's `MIN_INTERVAL = 1.5 s` is not to be tuned.**
Measured 2026-08-13: bursts of 10 at 1.5/1.0/0.6/0.3 s all completed with zero
throttling, and per-call time plateaus near 0.95 s because the request itself
takes ~0.9 s — so latency, not the interval, is the floor. **This is not
sufficient evidence to lower it**: throttling emerges under sustained load, not
in 10 calls, and network rate-limiting is designated untouchable.
*Override if:* a proper multi-hour soak test at a lower interval shows no
throttling — and even then, latency caps the gain at ~25%.

**D-7 — A zero is never accepted from a single shape. `ROOM_RETRY_BELOW` is
retired.**
The old `<10 rooms` gate declared a hotel finished at an arbitrary count while
further shapes were still finding rooms. Every hotel now runs the full base
ladder; anything still empty runs the escalation ladder.
*Override if:* never — this is the pipeline's core anti-silent-zero rule.

**D-41 — GuestNationality, Currency and multi-room `Rooms[]` are NOT extra probe
axes. Measured against a control; none beat plain repetition.**
The ladder swings `(weeks_out, adults, nights)` and holds three payload fields
constant on every call — `GuestNationality` ("PK"), `Children` (`[]`) and
`Rooms[]` (one object). Config's own comment claims nationality "drives which
rates return", so they looked like unexplored surface.

First pass looked spectacular: nationality SA +54 new room names, currency AED
+48, 3-rooms +32, across 6 hotels. **All of it was nondeterminism.** Bookme is a
sampler (D-5), so the control that matters is asking the *identical* question the
same number of times:

| 5 calls of… | new room names, 5 hotels |
|---|---|
| **the identical baseline (noise floor)** | **+234** |
| currency (AED/USD/PKR/EUR/SAR) | +232 |
| nationality (AE/GB/US/SA/IN) | +200 |
| `Rooms[]` × 2…6 | +64 |

Nothing beats repeating the same call, and multi-room is markedly worse. Adding
these axes would multiply the probe budget to buy variance we already get for
free. `Children` was rejected outright (non-200 for `[7]` and `[7,10]` — the
payload wants a different shape, unexplored, low prior).

**Retested on the population that actually matters — hotels holding ZERO.**
The table above measures *healthy* hotels, where "does this beat repetition?" is
the right question. On a hotel at zero it is not: repetition can only return zero
again, so nationality would not be competing with it, it would be the only thing
that could work. Supplier feeds are frequently market-gated, so this was a
genuinely open question and the first experiment did not answer it.

Measured directly, 2026-08-17, on hotels empty across every PK shape:

```
7 hotels answered-but-EMPTY   nationality (AE GB US SA IN RU CN)  rescued 0/7
                              currency    (AED USD EUR)           rescued 0/7
                              repetition  (control)               rescued 0/7
4 hotels HTTP 500 (D-8)       all three treatments                rescued 0/4
```

A hotel empty for a Pakistani guest is empty for all seven nationalities and all
three currencies. **The zeros are real** — a UAT inventory fact, not an artifact
of asking the wrong question — and D-8's "permanently unavailable" survives a
retest under seven nationalities.

**What remains true and useful:** on healthy hotels the nationality/currency
variants *do* return real room names we had not seen. They are simply a worse
buy than repetition at equal call cost, and their union with repetition was not
measured. So the open lever is still DRAW COUNT, not payload fields: measure
saturation as a function of how many times the ladder asks. Bookme probing is
~0.3 s/call at 8 workers with no observed throttling, so draws are cheap.
*Override if:* a saturation test shows shape diversity beating equal-cost
repetition, or someone finds the correct `Children` payload shape (all variants
tried returned non-200).

**D-42 — WHOLE-SYSTEM retest of D-41: nationality/currency variation is
confirmed not worth it, measured end to end. Operator's own bar (2x) not met.**
D-41 measured raw Bookme room-NAME yield. The operator's objection was exact and
correct: more room names that no source can put a picture on are empty rows, not
progress. Retested through the REAL pipeline (harvest → `match_hotel` →
`agoda_rooms` → `map_rooms` → `booking_fill`), on 5 healthy Dubai hotels,
baseline ladder vs baseline + 5 nationalities + 3 currencies:

```
                                base names  ext names | base imaged  ext imaged
pearl marina hotel apartment       17         20      |     19          23
anantara downtown dubai            72         72      |     64          64
anantara the palm dubai resort     79        100      |     78          94
hilton dubai the walk              74         74      |     62          60   <- WORSE
hilton dubai jumeirah               88        93      |     83          87
TOTAL imaged rooms: 306 -> 328 = 1.07x
```

Even where extra room names appeared (Anantara Palm +27% names), the imaged
count barely moved, and one hotel went DOWN (extra Bookme names compete for the
same Agoda/Booking candidates without adding new matchable inventory). 1.07x is
nowhere near the operator's 2x bar. **Confirmed: do not implement.**
*Override if:* a future whole-system measurement on a different sample clears 2x
— not raw name count, imaged-room count.

**D-43 — Thoroughly confirmed: hotels answering ZERO across the Bookme ladder
are real UAT inventory gaps, not an artifact of asking too few questions.**
Extends D-41's quick check with the "union" the operator asked for: 32 mixed
draws per hotel (date shape × nationality × currency, all combined) on 8
answered-but-empty hotels and 16 draws on 3 HTTP-500 hotels.

```
8 answered-empty hotels, 32 draws each  -> 0/8 rescued
3 HTTP-500 hotels,       16 draws each  -> 0/3 rescued
```

A hotel empty for a Pakistani guest is empty under every nationality, every
currency, and every draw count tried. The zero is safe to trust.

**D-44 — Booking's escalation is now ADAPTIVE: one shape at a time, ordered by
measured marginal value, stopping on a real plateau instead of a fixed list.**
Supersedes the fixed 3-shape (later 5-shape) escalation list from D-35. Measured
2026-08-17: an 8-shape saturation sweep on 10 geo-verified Dubai hotels found the
first two shapes alone capture 94% (90/96) of everything an 8-shape sweep ever
finds, and every shape past the fourth contributes ≤1 room in aggregate.

`BOOKING_PROBES` (now 2 shapes, was 1) runs unconditionally as base. Escalation
now runs shapes ONE AT A TIME from `BOOKING_PROBES_ESCALATION` (reordered
strongest-first: `(4,1) (8,1) (12,1) (2,2) (1,3)`), re-checking after each
whether the caller's actual gaps are covered via the real matcher
(`_booking_unfilled` → `map_rooms`, never a name-equality shortcut), and stopping
at `BOOKING_ESCALATION_STOP = 2` consecutive shapes with zero new photographed
rooms. A genuinely thin property is chased through the full ladder; a property
that plateaus after shape 3 is not charged for shapes 4-6.

Known tradeoff, on record: a hotel that happens to need exactly the 3rd
escalation shape after two flat ones is missed. That shape's aggregate value was
+1 room across 10 hotels, so the tradeoff was accepted for the calls it saves on
the more common already-plateaued case.

Caught a real bug while implementing this: the escalation loop's first version
passed `config.BOOKING_PROBES_ESCALATION`'s raw `(weeks, nights)` tuples straight
to `rooms_union` without `booking_shapes()`'s conversion to real dates — crashed
live (`int has no attribute isoformat`) the moment escalation actually ran. Every
existing selftest passed anyway, because the mock `rooms_union` ignored its
arguments entirely. Fixed, and the selftest now asserts the shape TYPE on every
call (base and escalation), plus a dedicated case that forces the escalation
loop through two flat shapes before a third succeeds — the exact path that broke.

**D-8 — Bookme HTTP 500 is permanent; transport faults are transient. Never
conflate them.**
25 consecutive calls across 5 hotels stayed 500, across 3 further dates each.
Retrying a 500 burns quota to re-learn the same answer; *not* retrying a
transport fault records a network hiccup as "this hotel has no rooms".

**D-9 — 8 workers is the Bookme concurrency bulkhead.**
Measured 0.52 / 3.76 / 4.61 hotels-per-second at 1/8/16 workers, zero throttling
at any level. Gains flatten past 8.

---

## Matching & writing

**D-45 — Recall raised via CORROBORATION, not a lower threshold; `class` and
`tier` bugs fixed as classes, not patched per instance. Tier/view demoted to
SOFT vetoes, overridable when corroborated.**
Operator decision, 2026-08-18 (CPO framing): *"it's better to have a photo of
at least a room than some random landmark."* The veto ladder was calibrated
when the alternative to a match was NO photo; it is not — the alternative is
the hotel-level lobby/landmark photo this project exists to remove.

**Why a threshold change was rejected first.** The operator supplied 11
labelled pairs — 7 that should map, 4 that should not. Their scores overlap
completely: should-map ranges 62.3–74.9, should-not ranges 62.5–72.0. No
`ROOM_ACCEPT`/`ROOM_REVIEW` value separates them, so lowering either was never
on the table. What *does* separate them in all 11 cases: whether any attribute
positively agrees beyond the bare class word (`match.corroborations()`) —
tier, bedroom count, bed overlap, or a shared non-generic token (a hotel's own
wing name, e.g. "Horizon", counts).

**Two real bugs found via the operator's new unmatched-CSV examples, fixed at
the class, not the instance:**

1. `features()`'s `class` was single-pick by `CLASSES` list order — the same
   array-position defect D-31/ARCHITECTURE.md's tier fix already closed one
   attribute over, missed here. `Family Suite Room` resolved to `suite`
   (precedes `room` in the list) and vetoed against `Family Room`, though
   neither side disagreed about anything — one just named two classes. Now
   set-valued (`classes`), vetoed only on **disjointness**. `Deluxe King
   Studio` vs `Studio Suite King Bed` is the same bug, same fix.
2. `TIERS` had no synonym table, so `premium`/`premier` (also
   `luxury`/`deluxe`, `classic`/`basic`/`value`/`essential`/`budget`/`economy`/
   `standard`) read as **disjoint** tiers rather than one rung spelled two ways.

**Three normalisation gaps closed**, all discovered from real supplier strings,
none patched as a literal string match:
- `PERK_NOISE` — service entitlements (`lounge`, `butler`, `afternoon tea`) are
  not room identity; a Club room is very often the same physical room as its
  non-club twin. Deliberately excludes `beach`/`pool`/view words — those are
  physical properties (caught live by the module's own view selftest).
- `PROMO_TRIGGERS` — marketing copy is free text and unenumerable, but it is
  always a **suffix**, so the name is truncated at the first trigger word
  (`off`, `valid`, `including`, …) rather than stripped word-by-word. Handles
  arbitrary length and vocabulary no list could anticipate: `"Twin Room- 50%
  off on Grand Hyatt Water Park & 20% off on Food and Beverage (Valid Until
  August 2026)"` → `"twin room"`. Note `"grand"` in that copy is also a TIER
  word — left in place it manufactures a tier disagreement out of an ad.
- `SPLIT_WORDS` — `"De Luxe"` (two tokens) never matched the `TIERS`
  vocabulary; four real review-band rows lost their only corroborating signal
  to this. Joined before any other processing.

**Soft vetoes** (`is_soft_veto`): `tiers` and `view` may now be overridden by
`config.ROOM_SOFT_VETO_RESCUE` when the pair is otherwise corroborated. `class`,
`bedrooms` and `beds` are **never** overridable — a suite is not a room, a
2-bedroom is not a 3-bedroom, a king is not a twin. The rescue direction is
favourable by construction: the source is typically the lower tier, so a
Superior's photo on a Deluxe room shows the guest no worse than they receive.

**One case deliberately declined, twice offered:** a bare tier word
(`"De Luxe"`, `"Premium"`) matches `Room`/`Suite`/`Studio`/`Apartment`
*identically* — there is no evidence in the name to choose one, so accepting
it is a coin flip, not a recall win. Selftested explicitly (`assert
room_match("De Luxe", "Deluxe Suite")[1]`) so a future "helpful" removal of
this guard is caught, not silently reintroduced.

**Accessibility is an explicit exception to corroboration.**
`corroborations()` returns `[]` outright when the two sides disagree on
accessibility, regardless of any other attribute agreeing — otherwise
corroboration would auto-publish exactly the pairs the accessibility score cap
(`ARCHITECTURE.md`, "Hard vetoes: disjointness, not inequality") is built to
hold back: showing a wheelchair user a standard bathroom, or hiding a
genuinely accessible room behind standard photos, is worse than a missing
picture. Caught live by `_selftest_mapping`'s own accessibility assertion the
first time corroboration was wired up — it turned a deliberately-capped
review-band pair into an auto-published one.

**Measured, not assumed:**

| | before | after |
|---|---|---|
| operator's 7 should-map examples | 0/7 auto | **7/7** |
| operator's 4 should-not examples | — | **4/4 still held** |
| 92-row review file (100-hotel run) | 0 auto | **78/92 (85%) auto**, 0 vetoed |
| 282-row unmatched-with-candidate | 0 rescued | **68/282 (24%) rescued** |
| like-for-like 14-hotel comparison | 58 unmatched, 7 review | **49 unmatched, 2 review** |
| room-image coverage, 25-hotel run | — | **93.4%** (up from 84.6% pre-change) |

All 11 operator labels plus the new unmatched examples are selftested by name
in `pipeline/match.py`'s `__main__`, both directions — a regression that
re-breaks `Captains Room` → `Classic Room` fails the build.
*Override if:* `ROOM_SOFT_VETO_RESCUE = False` restores the pre-2026-08-18 hard
tier/view vetoes exactly, for a full rollback without touching the rest of this
decision.

**D-10 — Rate-plan variants are grouped for MATCHING but published
INDIVIDUALLY, each receiving the imagery.**
Bookme sells one physical room under many names (refundable / package rate /
en-dash punctuation / bedbank echo). Consolidating them would fix one row and
leave the rest on their wrong photo. Verified in the DB: three punctuation
variants of one room, all three carrying the same thumbnail.
*Override if:* never — this is a business requirement, not an implementation detail.

**D-11 — A review-band match (62–75) is CSV-only; no `v2_rooms` row.**
Explicit product decision. Tradeoff on record: a room scoring 70 vanishes while
a room scoring 0 still gets a row. `config.REVIEW_BAND_CREATES_ROOM = True`
flips it.

**D-12 — `room_category_id` can never be NULL. Unmatched names fall back to
`General`.**
`classify()` only returns names from `categories.ALL`; `_validated_cat_ids()`
refuses to start a run unless `cat_ids` covers all of them; `_room()` falls back
to `General` loudly if a lookup ever misses.
*Override if:* never.

**D-12a — Every write to `v2_rooms` holds a per-hotel MySQL advisory lock.**
`db.publish()` takes `GET_LOCK('bookme_v2rooms_hotel_<id>', 30)` around the
existing-rooms READ *and* the INSERTs, because those together are the
check-then-act. `run.py`'s flock covers only `python -m pipeline.run` and is
skipped for `--dry-run`; it does **not** cover a standalone script importing
`pipeline.db`. That gap is not hypothetical — on 2026-08-13 a second process
wrote 18,251 rows across 431 hotels during a real run's probe phase, causing
three `1205` lock-wait timeouts and corrupting the run's own accounting.
A MySQL advisory lock was chosen over a file lock because it works across
machines, `GET_LOCK`/`RELEASE_LOCK` are SELECTs (so the additive-only guard is
unchanged), and MySQL frees it automatically when the connection dies.
Scope is **per hotel**, not global — the dedupe key is (hotel, room name), so a
global lock would serialise the pipeline for no safety gain.
Proved in `pipeline/db.py`'s selftest against a real second connection.

**D-13 — Database writes are additive-only, except a narrow COALESCE backfill
for `thumbnail` and `size_sqft`.**
`pipeline/db.py::_sql()` refuses anything that is not SELECT/INSERT/SHOW.
Approved exceptions to date: the `size_sqft` column migration, and the
2026-08-13 `v2_rooms`/`v2_attachments` cleanup — both run in dedicated
one-time scripts *outside* the guard, never by weakening it.

**D-14 — Bookme cannot supply `size_sqft`; Agoda is the only source.**
Probed the full availability payload for `sqft`, `m²`, `size`, `Area`,
`RoomSize`: **0 hits**; room `Amenities` empty on 118/118 objects.
*Override if:* Bookme adds the field.

**D-15 — Never borrow images from a same-category room to fill an unmatched
room.**
That would put a plausible-but-wrong photo on a room — precisely the defect this
project exists to eliminate.

---

## Operational

**D-16 — The ledger is append-only CSV, last-row-wins per hotel.**
Operator-readable by request, and it lives outside Bookme's schema.
Loader must tolerate schema drift: a column was once added to `*_COLS` while the
on-disk header stayed narrower, silently shifting every new row by one and
putting run ids in the `reason` column. The loader now matches each row by its
own width.

**D-17 — `LEDGER_STALE_DAYS = 365`.** Room imagery is identity, not
availability — it changes on refurbishment timescales. A hotel still owing an
image is flagged `needs_image_backfill` and comes back sooner regardless.

**D-18 — Agoda property cache is mid-run scaffolding, swept on clean
completion, kept when stopped early.**
⚠️ Known limitation (2026-08-13): it caches geo-**rejected** candidates too, with
no flag distinguishing them. **Never derive statistics from this cache** — it
contains properties in Perth, Las Vegas and Portland from Dubai matching runs.

---

**D-19 — Room name, photo and `size_sqft` are IDENTITY facts; availability is a
STATE fact. Never let a state fact gate the acquisition of an identity fact.**
Agoda's room grid answers "what is bookable on this date"; we need "what rooms
does this hotel have". A sold-out room still has a name and photographs.
Consequence: identity facts are safe to union across probes/dates (and are);
state facts must never be merged across moments.
**Corollary — "a hotel with zero rooms" is domain-impossible.** When the system
produces it, that is an open question, not a fact: escalate the conditions, and
if it survives, record the *reasoned negative* (what was tried) rather than an
unexamined zero. Measured for Agoda: 24 hotels × 9 date shapes to +50 weeks →
0 rescued, and the sold-out payload carries no room-type identity at all. See
`DIAGNOSIS.md` §8b.

**D-20 — Never use `agoda.suggest(name)[0]` without geo-verification.**
For "Carlton Tower Hotel" the top candidate is the **Kuwait** property (10
rooms), not Dubai (0 rooms). An ad-hoc script that takes `[0]` silently measures
a different hotel — this produced a convincing but entirely false "7 of 24
rescued" during the 2026-08-13 audit. `match_hotel()` gates on distance and does
this correctly; analysis scripts must too.

**D-21 — A hotel lost to a transient fault is re-queued, not abandoned.**
It has already paid for its Bookme probes, Agoda match and escalation ladder --
the most expensive work in the run. `db.TRANSIENT` and `WriteLockTimeout` earn
one deferred retry at the end of the run; anything else is a real bug and is
errored immediately so it is not hidden behind a duplicate error line. Capped at
one retry per hotel — a permanently-sick hotel must not spin the loop.

**D-22 — Do NOT add `UNIQUE(v2_common_hotel_id, name)` to `v2_rooms`.**
Approved in principle, then **rejected on measurement**. `v2_rooms` is
`utf8mb4_unicode_ci`, which folds **accents as well as case** — verified:
`'Millesime' = 'Millésime'` → `1`. Our dedupe uses Python `.lower()`, which folds
case only. Bookme really does return both spellings of the same room:

```
'Luxury Room, Club Millesime Access, 1 King Bed, Burj Khalifa View'
'Luxury Room, Club Millésime Access, 1 King Bed, Burj Khalifa View'
```

Consequences: the DDL fails outright on that existing pair, and if forced, the
next time both spellings appear the second INSERT raises a duplicate-key error
and **rolls back the whole hotel's transaction — losing every room for it.**
That is strictly worse than the duplicate it prevents.
*Override if:* the index is built on an accent-sensitive key — e.g. a stored
generated column `UNHEX(SHA2(name,256))` compared as binary — **and** the Python
dedupe is changed to fold identically, so the two can never disagree.

**D-23 — Booking.com is a viable SECOND room-image source, and its per-room
binding is explicit.** `window.booking.env.allRoomPhotos` is clean JSON:
```json
{"id":"112496932","thumb_url":"…max200/112496932.webp",
 "large_url":"…max1024x768/112496932.webp","associated_rooms":["4884134"]}
```
`associated_rooms` binds a photo to a room-type id, and `hotelPhotos` is a
**separate** array — Booking.com itself distinguishes room photos from
hotel-wide photos, which is precisely the defect this project exists to fix.
Measured on Park Hyatt Dubai (a hotel Agoda returns **0** rooms for): 17/17
named rooms photo-covered, **78/78 room photos carrying an association**, plus
45 hotel-level photos kept apart.
**Constraints:** plain HTTP is blocked (HTTP 202 anti-bot) — a real browser
session is required; and it is **availability-scoped like Agoda** (on a sold-out
date `allRoomPhotos` is empty while `hotelPhotos` survives).

**D-24 — Booking.com is reachable over PLAIN HTTP. No browser needed to fetch.**
The `HTTP 202` is an **AWS WAF** challenge, not a header problem. Replaying one
cookie — `aws-waf-token`, captured once from a real browser — turns
`202 / 3,962 bytes` into `200 / 3.7 MB`. Two traps, both of which *look* like a
block: request `gzip, deflate` and **never `br`** (undecodable brotli reads as
"every marker absent"), and remember room names are absent from the served HTML
attributes (`data-room-name=""`) — they live in JSON as `b_name`.
Implemented in `pipeline/booking.py`; token in `.env`.
Measured: **3.84 s/hotel** (fetch 3.83 s, parse 15 ms, 4.2 MB).

**D-25 — Parse Booking's room objects self-validatingly; NEVER positionally.**
Room names sit in `{"b_id":N, "b_blocks":[…thousands of chars…], "b_name":"…"}`.
Three approaches were tried and each failed **silently**:
anchoring on `{"b_id":` missed 8/17 (b_id is not always first); walking backward
counting braces broke on a `}` inside a string; a single forward string-state
pass over the whole page desynced (the page is HTML, not JSON — its quotes do
not pair). The working method extracts candidate objects and **accepts one only
if `json.loads` succeeds**, so the boundary is proven rather than assumed.
Contract, enforced by selftest: **a room may be MISSING, never MISLABELLED** —
measured 16/17 named, 0 wrong, against live-DOM ground truth.

**D-36 — Measured contribution of the gap-fill: 52% of remaining gaps closed,
room-image coverage 86.2% → 93.4%.**
Dry run, Dubai, 2026-08-17, on the 17 hotels that completed with a healthy
Agoda: **348 rooms, 48 without a photograph after Agoda, 25 of those filled by
Booking**, across 6 hotels. Of the 9 hotels that actually had gaps, **9/9
resolved on Booking** under the D-31 gates — better than the 75% measured on the
earlier 20-hotel identity sample, and consistent with it (different sample,
small n). Two hotels were carried almost entirely: `ascot` 10/11 gaps, and
`admiral-plaza` 7/7. Eight of the 17 hotels had **no gaps at all**, so the
second source cost nothing for them.

⚠️ **The A/B did not complete, and the run-total comparison against the
Agoda-only baseline is INVALID.** Two back-to-back 50-hotel runs exhausted
Agoda's tolerance: from hotel 24 onward every hotel returned
`suggest failed: HTTP 502`, with the 420 s cooldown failing to clear it (1–2
hotels, then throttled again). Run B was stopped deliberately rather than left
to spend another hour generating 502s. The baseline covered 37 hotels and run B
17, so **their run totals describe different hotel sets and must not be
subtracted from each other.** The numbers above come from `booking_fill.csv`,
which is per hotel and therefore immune to that confound.
*What this costs:* nothing about Booking is unmeasured — only the head-to-head
run-total framing is missing. To get it, re-run both **with a gap between them**
so Agoda is not already throttled, or on disjoint hotel sets.

**D-41 — The per-hotel log line explains it when `rooms published > agoda
rooms`, instead of leaving the reader to work it out.**
`agoda=N` counts distinct PHYSICAL rooms Agoda has photos for; a published row
is one per Bookme SELLABLE NAME, and Bookme routinely sells one physical room
under many names (rate plans, refundable/non-refundable, package-rate suffixes
— D-10). Verified live on Hilton Dubai Jumeirah: 19 Agoda rooms fed 18 distinct
image sets, reused across 58 of 62 Bookme names — correct, not inflated. Read
cold, `agoda=19 -> 58 rooms` looks like a broken or duplicated count, which is
exactly what an operator watching prod scrollback should never have to
puzzle out for themselves. The line now appends
`[N bookme names share M agoda room photos -- rate-plan duplicates, not an
error]` whenever the counts diverge, and stays exactly as before when they
don't — no cost on the common case where they already agree.

**D-40 — No "thin hotel" or "Agoda shortfall" trigger. Considered, rejected as
unnecessary.**
Proposed: run Booking when a hotel ends up with very few rooms, or when Bookme
sells ≥1.5× what Agoda showed. Rejected because it builds machinery for work
Booking is not permitted to do. Booking may never introduce a room type (D-34),
so the only thing it can contribute is imagery for a room **Bookme already
sells** — and every such room that Agoda failed to match is already emitted by
`map_rooms()` as a row with no images, which is already a gap, which already
sends the hotel to Booking. The proposed trigger would fire in exactly one
extra case: a hotel whose few rooms are *all* already photographed, where there
is nothing Booking is allowed to add.
Asserted rather than left to be re-derived: the selftest builds a hotel where
Bookme sells 4 rooms and Agoda matched 1, and requires all 3 misses to be
filled by Booking, the room count to stay at 4, and Agoda's own match to be
untouched.
*Override if:* D-34 is ever relaxed to let Booking contribute room types — then
a discovery trigger becomes meaningful, and not before.

**D-37 — Booking covers what Agoda could not; Agoda remains the first layer.**
Operator decision, 2026-08-17: *"what the team cares about is the output — and
output demands booking to take over where agoda failed."* Clarified by the same
operator: *"agoda is the first layer... we will use agoda's images for our first
mapping and maximum of what our pipeline was already doing."* Agoda is tried for
every hotel, runs its full escalation ladder, and its images always win; Booking
is only ever offered a room left with no photograph.
Previously the hotel loop `continue`d on `no_agoda_match` before `map_rooms()`,
so those hotels published **nothing** — not even imageless rows for Bookme names
already harvested. That was **9 of 50 hotels (18%)** of the baseline: the extreme
case of "no Agoda match" was the one case the second source was never asked
about, purely because of where the `continue` sat.
Now the branch falls through with `ag_rooms = []`, so the same `map_rooms()` +
`booking_fill()` path runs with Booking as the *only* image source. Verified
live: `al waleed palace hotel apartments oud metha` had no Agoda match and
published **4 rooms** where it previously published 0.
Deliberately reuses the existing mapping path rather than adding a parallel one
— the vetoes and the position-independent assignment must not exist twice.
Two floors preserved: a hotel with no Bookme rooms *and* no Agoda match still
`continue`s (there is nothing to publish), and the `no_agoda_match` reason is
still written to the ledger and `hotels_to_revisit.csv` even when Booking
rescues the hotel — a successful publish clears it, so the record stays true
either way.

**D-39 — CORRECTION: most `v2_rooms` rows are written by BOOKME, not by us.
Reading room names is a write.** *(overturns `DIAGNOSIS.md` §1)*
Proven 2026-08-17 with a one-call experiment: on a hotel with **0** rows, a
single `bookme.availability()` call — no pipeline, no `db.publish()` — created
**5 rows**. Repeating with other date shapes grew them 5 → 9 → 12: Bookme's
writer is idempotent by name and accumulates the union of room names it
discovers. Its rows carry `hotel_id NULL` and `room_category_id NULL`, a
signature `db.publish()` cannot produce (it always writes `hotel_id=0` and a
resolved category).

Consequences, all previously misattributed:
* The 18,251 uncategorised rows were **not** an "uncoordinated second writer"
  or a concurrently-edited copy of this code. Our probe ladder asks that
  endpoint ~28× per hotel across 1,340 hotels; the rows are the footprint of
  our own *reads*.
* **A `--dry-run` still causes rows to appear.** Nothing in this repo wrote
  them — verified before/after on a dry run: 55 → 58. Anyone monitoring the
  table during a dry run must expect this, or they will conclude the dry-run
  guard is broken. It is not.
* It is why coverage looked catastrophic: we only ever set a category on rows
  **we** insert, and most rows are Bookme's. Fixed by extending the COALESCE
  backfill in `_fill_empty_fields` to `room_category_id` — fill-only-if-NULL,
  never an overwrite, same narrow exception already granted for `thumbnail`
  and `size_sqft` (D-13).
* That change exposed a second, smaller defect its own guard test caught: the
  backfill's `WHERE` was static, so it fired on rows whose only empty column
  was one the call had nothing for — changing only `updated_at` while returning
  True. The condition is now built from what is actually offered.

*Not fixable by us:* we cannot read room names without triggering Bookme's
write. Any "leave the database untouched" claim must be scoped to *this
pipeline's* writes, which remain additive-only.

**D-38 — "We could not ask" is never recorded as "there is no match", and a
dead upstream stops the run instead of quietly consuming it.**
The production failure this closes, observed 2026-08-17: Agoda blocked the run
at hotel 24 of 50; every subsequent hotel was written up as `no agoda match`
while the log looked healthy. An unattended city run would have burned **five
hours** producing empty results that are indistinguishable, in every report,
from genuine negatives. Three separate defects, three fixes:

1. **A fixed cooldown cannot outlast a longer block.** `_note()` reset its
   counter after each stand-down, so the run looped *6 failures → sleep 420 s →
   6 failures → sleep 420 s* forever, at ~2 hotels per cycle. The stand-down now
   **doubles** per cooldown that produced no success (420 → 840 → 1680 …, capped
   at 3600 s) and resets on the first success. Selftested with `time.sleep`
   stubbed, asserting both the growth and the reset — a livelock is exactly the
   kind of bug that survives review by looking like it is working.
2. **The reports lied.** `match_hotel()` now returns `unreachable: True` when
   the candidate list is empty *because the request failed*, and the loop
   records `agoda_unreachable`, counts it separately, and never reuses the
   `no_agoda_match` label for it. Same distinction this pipeline already makes
   between a Bookme 500 and a network fault.
3. **Nothing ever stopped.** A circuit breaker counts consecutive *unreachable*
   hotels: at 3 it prints a banner and **rotates the Agoda session** (fresh
   cookies/identity sometimes clears a soft block), at 8 it stops the run. By
   then the escalating cooldowns have already spent ~an hour standing down, so
   the abort means genuine unavailability, not a blip. The run is labelled
   `-AGODA-DOWN` in the folder name and `stopped_by_agoda_breaker` in the
   manifest, so a truncated run can never be mistaken for a complete one.

Crucially the run is **not** wasted while Agoda is down: those hotels skip the
Agoda fetch entirely and go to Booking (D-37), so the pipeline stops hammering a
host that is refusing it while still publishing.
*Override if:* the breaker fires on a healthy host — raise `AGODA_SICK_ABORT`
rather than removing it, and record why.

**D-29 — Geo verification is MANDATORY on every Booking acceptance. There is no
name-only fast path.**
An earlier design had a free "tier 1" that accepted a high `strict_score`
without checking distance, on the belief that `strict_score` (order- and
length-sensitive) was subset-proof. It is not — measured, it scored
`hilton dubai the walk` ≥97 against a listing it had no business accepting.
Every wrong match ever measured in this module came from a path that skipped
geo, so the free tier was deleted rather than tuned. Operator decision,
2026-08-16: *"just add geo check on every hotel from booking"*.
Cost of the guarantee: one property-page fetch per surviving candidate — which
D-31's name gate then reduced to roughly one per hotel, and zero for hotels
Booking does not carry.

**D-30 — ACCEPTED RISK: a Booking listing may be one private apartment inside
the right building.**
Booking sells individually-listed apartments alongside hotels, and geo cannot
tell them apart — `pearl marina hotel apartment` resolves to a private
2-bedroom listing metres away. Operator decision on record, 2026-08-16:
*"let's forsake the private listing (it's the same hotel at least — we don't
care about that — the rooms will be the same)"*.
Recorded with its residual concern rather than as a clean pass: the listing's
**room set** is that one apartment, not the hotel's catalogue, so its room
*names* may not correspond. D-34's "never invent a room" rule is what bounds
the damage — such a listing can only fill a room Bookme already sells, under a
name that had to pass `map_rooms()`'s vetoes.
*First place to look if* wrong-room imagery ever appears on apartment-style
properties.

**D-31 — Booking identity requires BOTH a name gate and a distance gate.
Neither alone is sound, and they fail in opposite directions.**
Settled 2026-08-17 by measuring every candidate the resolver considered across
14 Dubai hotels:

| population | best name score | distance |
|---|---|---|
| correct matches (11) | **100.0** every one | 0.028 – 0.103 km |
| wrong candidates | ≤ 72.7 | ≥ 0.360 km |

Two live failures, each defeating one gate:
* **name alone** accepted `hilton dubai the walk` →
  `hilton-dubai-jumeirah-residence` (token_set_ratio scores a subset 100).
* **distance alone**, at the old `max_km = 1.0`, accepted `pearl marina hotel
  apartment` → `lotus-grand-apartments-spa-marina` — *a different hotel* — in a
  live run. In a dense district 1 km proves a neighbourhood, not a building.

Set to `BOOKING_MIN_NAME = 90`, `BOOKING_MAX_KM = 0.25`: inside both gaps, not
at their edges. Re-tightening the *name* alone was already tried and only
converted wrong answers into missing ones (D-27) — the distance gate is what
made a high name bar affordable.
Side effect worth noting: at MIN_NAME 90 a hotel Booking does not carry leaves
**no** candidate above the floor, so it costs one search and **zero** property
fetches. Per-hotel gap-fill cost fell from ~48 s to ~11 s as a direct result.
*Override if:* a larger sample shows correct matches below name 90 or beyond
250 m — record the case, don't just widen the constant.

**D-32 — Scores are computed against the candidate's TITLE, never its slug.**
Booking slugs are historical: `hilton-dubai-jumeirah-residence` is today titled
"Hilton Dubai The Walk", and `mapvenpick-jumeirah-lakes-towers` (sic) is
"Mövenpick Hotel Jumeirah Lakes Towers". A slug that looks like the wrong hotel
routinely is not. This is why D-27 recorded that pairing as a wrong match and
D-31 does not — the *reasoning* was unsound, the *answer* happened to be right.
Never eyeball a resolution report by slug alone.

**D-33 — The WAF token is minted by the pipeline itself. There is no operator
step.**
Supersedes the manual `.env` half of D-28. Measured 2026-08-17: headless
Chromium (already a dependency, already driven by `agoda_browser.py`) loads a
property page, the AWS WAF challenge JS runs, and the `aws-waf-token` cookie
appears **4.1 s** later. The token is **portable** — replayed from a plain
`requests.Session` it returns HTTP 200 and 1.9 MB of real markup — so the
browser is paid for once per token, not once per fetch.
No TTL constant is guessed anywhere: the token is cached on disk and used until
a request actually returns 202, which re-mints in place and retries that same
request. Expiry is therefore ordinary self-healing operation. `Blocked` is now
raised **only** when a token minted seconds ago is also refused, which means
Booking's posture changed rather than a cookie ageing out.
`BOOKING_WAF_TOKEN` survives as an optional seed for locked-down environments;
a stale one costs exactly one 202 before minting takes over. The cache lives at
`out/booking_waf.json`, deliberately **outside** `out/cache/`, which a completed
run sweeps.
Selftest runs against a deliberately poisoned token, so it tests the recovery
path rather than the happy one.

**D-34 — Booking fills gaps; it never replaces an Agoda match and never invents
a room.**
It runs after the Agoda mapping, scoped to rooms left with no photograph, so a
hotel Agoda covered fully costs nothing. Booking room types with **no** Bookme
counterpart are dropped rather than published — the opposite of the Agoda path,
deliberately: an accepted Booking listing may legitimately be one apartment
inside the building (the accepted risk in D-30), whose room set is not the
hotel's. Filling a room Bookme already sells cannot introduce a room that does
not exist; adding one can.
Matching is delegated to `map_rooms()` rather than reimplemented, so the veto
rules and the position-independent assignment cannot drift into a second copy.
`image_source` (`agoda` / `booking` / empty) records provenance per room, and
`booking_fill.csv` records one line per hotel asked — including the ones it
could not help, with *why*, since "no gaps", "unresolved", "no rooms" and "no
match" are four different facts a bare zero would flatten.

**D-35 — One base date shape, escalating only on zero.**
Measured over 8 resolved hotels: the property page already fetched for identity
gives 0/10/14/14/0/22/5/5 photographed rooms for free (parsing costs ~0 ms —
100% of the cost is the fetch). One dated shape on top rescued a hotel from
0 → 7 rooms. A **second** blanket shape added 2 rooms across all 8 hotels for
~48 s, so it was demoted to `BOOKING_PROBES_ESCALATION`, which runs only for a
resolved property still showing zero. Same rule as the Bookme ladder — a zero is
never accepted until every shape has been asked — but the asking is paid for
only where the zero actually is.

**D-28 — The Booking WAF token is SHORT-LIVED. Any long run must re-mint
mid-flight.** *(the manual half is superseded by D-33)*
Measured 2026-08-15: a token minted at the start of a working session was
already rejected later in that same session (`202` on `searchresults.html`
while property pages still worked). Token expiry is therefore **normal
operation**, not an error state. `Blocked` now says so and tells the operator
how to re-mint. A production integration needs either a browser step that mints
on demand, or a run that pauses and asks — it must not treat expiry as "this
hotel has no rooms", which is the unearned zero this pipeline refuses.

**D-27 — Booking identity CANNOT be resolved by name matching alone. The geo
check is load-bearing, not an optimisation.**
Measured over three attempts on 30 Dubai hotels:

| approach | resolved | known-wrong survivors |
|---|---|---|
| slug derived from DB name | 4/18 (22%) | 0 (fails loudly — wrong slug 302s) |
| search + `score` (token_set_ratio) | 21/30 (70%) | ≥2 |
| search + `strict_score ≥90` | 15/30 (50%) | ≥1 |

The middle row mis-resolved `hilton dubai the walk` →
`hilton-dubai-jumeirah-residence` at **"100%"**, because `match.score` is
token_set_ratio and rates a SUBSET as perfect. Tightening the threshold only
converts wrong answers into missing ones — it never reaches correct.
**Root cause:** `"latitude"` sits on the search page *outside* the
property-card markup, so candidates arrive with no coordinates and the distance
gate never runs. Fix: read coordinates from the top 1–2 candidates' own property
pages (~3.8 s each, affordable at 1–2, not at 25), then gate on distance the way
`match_hotel()` does for Agoda.

**D-26 — Do not wire Booking in until identity resolution clears 80%.**
Deriving the Booking slug from the DB hotel name resolves **4/18 = 22%**
(several DB names are also truncated). A wrong-hotel photo is worse than a
missing one, so the gate holds. `searchresults.html` (same WAF token) returns
25–26 candidates with the right property often first but **not reliably** — it
needs the geo+name verification `match_hotel()` already implements.
*Override if:* verified search resolution measures ≥80% on a 30-hotel sample.

**D-46 — The pipeline is now TWO PHASES: discover everything, then commit
everything. A gate sits between them.**
Operator observation: watching the CLI during the old single-loop pipeline
looked "stuck" for long stretches, because a hotel with a big gallery blocked
the NEXT hotel's matching behind its own image downloads -- Agoda/Booking
identity resolution is cheap network calls, mirroring is real image bytes, and
interleaving them per-hotel meant the operator was watching image transfer
disguised as a hang.

**Phase 1 (discover):** `match_hotel` → `agoda_rooms` → `map_rooms` →
`booking_fill`, for every hotel. No image downloaded, nothing written to
MySQL — exactly what `--dry-run` already computed, just no longer entangled
with Phase 2. Each hotel's result is check-pointed to `out/cache/plan.csv`
the moment it's mapped (`_append_plan_rows`), fsynced, one hotel at a time —
same durability discipline as `ledger.py`'s own appends. **Phase 2 (commit):**
reads the plan back, re-fetches `existing_rooms()` FRESH (never trusts the
Phase 1 snapshot — time may have passed, another run may have written), mirrors
only what's still missing, publishes. `db.publish()`, `cos.mirror()`,
`ledger.py` and the hotel write lock are **unchanged** — only what feeds them
moved earlier.

**The gate:** the moment Phase 1 ends, the exact `_print_human_summary()` this
pipeline already prints at the end of a run is printed again, right there —
free, since every number it reads (`rooms_with_candidate_images`, etc.) is
already fully populated by Phase 1 and Phase 2 never touches those counters.
A dry run always proceeds (Phase 2 writes nothing in that mode); `--plan-only`
always stops; otherwise `--yes` or a non-interactive stdin proceeds
automatically (an unattended cron job must never block on `input()`); an
attached terminal is asked `[y/N]`.

**ACID audit, before implementing, not after:** `db.publish()` reads only
`hotel["id"]` — a plan-reconstructed hotel dict is already sufficient, proven
because `apply_review_decisions()` (existing, shipped) already calls it that
exact way. `cos.mirror()` is explicitly documented as orphan-safe under a
crash: "the next run computes the same key and reuses it" (content
addressing). One real gap WAS found and deliberately not inherited:
`apply_review_decisions()` mirrors unconditionally on every call, so resuming
it re-downloads images for already-committed rooms (safe, wasteful). Phase 2
instead reuses `_split_for_mirroring()` + a fresh `existing_rooms()` read, so a
resumed commit never re-touches a room already in MySQL.

**Resumability, live-verified, not just reasoned about:** `--plan-only` run →
checkpoint written, folder tagged `-PLAN-ONLY`. Re-invoking the SAME command
without any flag printed `plan checkpoint: 3 hotel(s) already discovered,
resuming` and skipped Phase 1's per-hotel loop entirely, landing straight on
the gate. A torn last row (crash mid-write) is dropped by an explicit width
check, not silently accepted with `None`-filled fields — `csv.DictReader`
does NOT do this on its own; verified by writing one by hand and reloading.
Selftest (`_selftest_plan_checkpoint`) additionally proves: image/size/category
round-trip correctly, `image_source` provenance survives (a Booking-sourced
room must not read back as `"agoda"`, which is `_room()`'s own default for any
non-empty image list), and the plan scope is invalidated by BOTH a different
hotel set and a changed matching config (`BOOKING_MIN_NAME` etc.) — reusing
rooms matched under stale tuning would silently publish a decision the current
config disagrees with.

Category is deliberately **re-derived at commit time**, never trusted off the
CSV: `_room_from_plan_row` calls `_room()` on the room's plain name against
`cat_ids` as resolved in the Phase 2 invocation, exactly like
`_room_from_review_row` already does. If `cat_ids` changed between discovery
and commit (a category renamed, `categories.classify()` updated), the commit
reflects the current rules, not stale ones baked into the plan file.

*Override if:* never, without re-measuring — this closes a real, reported
"looks broken but isn't" observability gap and the audit specifically found
and closed the one wasteful-resume gap the closest existing precedent had.

**D-47 — A resumed Phase 2 must skip hotels already committed, not just avoid
corrupting them.**
D-46 made a crash mid-commit *safe* to resume (re-mirroring an already-imaged
room is a no-op, re-publishing is COALESCE-guarded) but not *quiet* — the
original Phase 2 walked every hotel in the plan unconditionally, so a resumed
run re-queried and re-printed a line for every hotel already done, burying the
one signal that matters (what's still pending) under noise. For an operator
who was explicitly told "just re-run it, you don't need to look at anything,"
noise defeats the point as much as a wrong answer would.
Fixed by extracting `_phase2_skip_ids(led)` (= `fresh_ids() - unresolved_ids()`,
the exact same nuance the top-level hotel selection already uses — a hotel
published but still owing an image backfill is NOT skipped) and filtering
Phase 2's `work2` against it, reading the ledger fresh at Phase 2's own start
rather than trusting the value computed before Phase 1 ran.
**Live-verified with a real `kill -9`**, not just reasoned about: started a
real (non-dry) run, killed it mid-download after 2 hotels had committed,
re-ran the identical command with no flags changed and nothing inspected
first. It printed `1 hotel(s) in the plan are already published and resolved
-- resuming with the remaining 6` and finished cleanly, `[ok]` on both
reconciliation checks. One honest nuance surfaced by the same test: `--limit
N` selects "the next N hotels still needing work," which is *dynamic* — if
the killed run fully resolved one hotel before dying, the resumed
invocation's "next N" can shift by one member, and the plan checkpoint
correctly refuses to reuse a plan built for a different hotel set (re-fetches
via Agoda/Bookme/Booking rather than silently reusing stale data). No MySQL or
COS work was lost or duplicated either way — only some network calls were
re-spent. This is the direct argument for D-48's `--slugs`: an explicit,
stable hotel list doesn't drift, so a resume on it reuses the checkpoint
perfectly.

**D-48 — Three new ways to target a run: `--slugs`, `--slugs-file`, and a
wizard range question. A raw database id-range flag was proposed and
deliberately NOT built.**
Operator ask: run against named hotels without any city context, and select a
*position range* within a city's hotel list ("hotels 1000 to 1564") rather
than only "the first N."

`--slugs SLUG1,SLUG2,...` / `--slugs-file PATH` (unioned if both given) look
hotels up directly (`db.hotels_by_slug`, identical row shape to `db.hotels()`
so nothing downstream needs a special case) and bypass city scope AND the
ledger-freshness skip entirely — naming a hotel is a request to process it
*now*, not a smaller city sweep. Mutually exclusive with `--city`/`--city-id`/
`--limit`/`--offset`/`--random` (argparse-enforced), since those only mean
anything as part of choosing a city.

The range: `db.hotels()`'s own SQL is `WHERE city_id IN (...) ORDER BY id` —
`v2_common_hotels.id` is a **global** auto-increment shared across every city,
so "hotel id 1000–1564" and "the 1000th–1564th hotel of Dubai's own list" are
different, usually very different, sets. The operator's own stated intent
("limit 10 would process first 10, so on... run 2500 to 3000") was clearly the
second — a **position** within the already-city-filtered, id-ordered list —
so that's what got built: `--offset N` (needs `--limit`, incompatible with
`--random` — offsetting into a per-run-reshuffled list isn't a stable range),
plus the SAME range as a natural wizard question (`'1000-1564'` or
`'1000:1564'`, parsed by `_parse_bound_reply`) rather than only a bare flag an
operator would need to already know exists.
A literal `v2_common_hotels.id BETWEEN MIN AND MAX` flag (city-agnostic, a
different tool — a whole-table sweep unrelated to city boundaries) was
proposed and explicitly not built, because it wasn't actually what was asked
for. Build it only on a fresh, explicit ask.
Also flagged, not built: if the *motivation* for range-slicing is running
several processes in parallel for speed rather than just targeting a slice,
`out/.run.lock` currently blocks a second simultaneous invocation entirely —
that needs the lock changed to per-slice, a separate and bigger piece of work.

**D-49 — Wizard range is 1-indexed and INCLUSIVE of both ends. Fixed a real
off-by-one from D-48.**
As shipped, `_parse_bound_reply("6-7")` computed `(bound=1, offset=6)` — a
Python 0-indexed slice `todo[6:7]`, which is ONE hotel (the 7th), not the two
a person means by "6 to 7." Caught by the operator asking directly rather than
by review. Fixed: `"A-B"` now means the Ath through Bth hotel, both included
(`end - start + 1` hotels, `start - 1` as the 0-indexed slice offset); `"A-A"`
is a valid single-hotel selection, only `end < start` or `start < 1` is
rejected. The raw `--offset`/`--limit` CLI flags are UNCHANGED (still 0-indexed,
SQL `OFFSET`/`LIMIT` convention) — this fix is scoped to `_parse_bound_reply`
only, which nothing else calls, so the two conventions (flag = SQL-familiar,
wizard = how a person reads a range aloud) stay deliberately different for
their deliberately different audiences.

**D-50 — The download/publish confirmation gate can be pre-answered ONCE, up
front, before discovery even starts.**
Previously an operator who already knew they wanted a hands-off run still had
to sit through the whole discovery phase to discover a second prompt was
coming. `_wizard()` now asks first, before the city question: *"Pause and let
you review the discovered plan before downloading starts? [Y/n]"*. Answering
'n' sets `a.yes = True` — reusing the flag's EXISTING meaning rather than a
parallel flag that could quietly drift from it — so the later gate (D-46) is
skipped automatically. Live-verified: piped `n` as the first answer, no
`Proceed to download...` prompt appeared, the run went straight through to
completion.

**D-51 — Production post-mortem, run `20260819-063516`: five independent
failures, only one of them about match quality.**
15.9 hours, 821 of 2,916 hotels reached, **zero mapped**. Bookme was flawless
throughout (2,916/2,916 answered on all 28 shapes, 0 errors, 0 unavailable,
~24k room names), and the manifest confirms `bookme_api_base` was **prod** —
so this was not an environment problem. Each failure below is fixed, with the
evidence that drove it:

1. **221 hotels destroyed by a read that did not need to be fatal.** 100% of
   the MySQL drops were on the per-hotel `existing-room check`, 100% resulted
   in the hotel being deferred, and the denominator visibly inflated as they
   requeued (`[336/3183]` → `[820/3404]`). That read only SCOPES the Booking
   gap-fill — Phase 2 re-reads the truth before writing — and prod has **0
   v2_rooms rows for Dubai**, so it was scoping against an empty table. Now one
   bulk `existing_rooms_bulk(city_ids)` before discovery, non-fatal on failure.
   2,916 queries → 1, and a DB blip can no longer discard completed work.
2. **The circuit breaker was unreachable on the dominant path.** `if not
   bm_rooms: continue` fired *before* the breaker check — 207 hotels took
   exactly that branch — and a plain `no_agoda_match` reset `agoda_sick` to 0,
   so interleaved rejections kept the counter from ever accumulating. Six
   escalating cooldowns (3.75 hours of sleeping) and the breaker never once
   evaluated. Breaker now runs on every path; only an ASKED-AND-ANSWERED
   negative clears the counter.
3. **The matcher stopped looking while in the wrong city.** See D-52.
4. **`size_sqft` does not exist on prod.** See D-53.
5. **The run ended in an unhandled `pymysql.err.Error: Already closed`**, so a
   completed run exited non-zero and looked like a crash. Guarded.

**VALIDATED LIVE against prod, 2026-08-20** — 40 Dubai hotels, `--plan-only`,
nothing written:

| | first prod run | after these fixes |
|---|---|---|
| hotels mapped | **0** of 821 | **33 of 40 (82%)** |
| rooms with a photo | 0 | **833 of 916 (91%)** |
| MySQL drops / deferrals | 221 | **0** |
| errors | 0 (nothing got far enough) | **0** |
| booking rescues | 0 | **196 rooms across 10 hotels** |
| throughput | 15.9 h → 0 results | 30.9 min for 40 = **46 s/hotel** |

The `-> mapped` line, entirely absent from 1,062 lines of the production log,
now appears for 33 of 40 hotels. Of the 7 not published, 6 were `listed but
zero rooms` on *both* platforms and 1 had no listing anywhere — genuine
negatives, each reached only after the full 28-shape Bookme ladder and Agoda's
own escalation.

⚠️ **Throughput implication, stated plainly:** 46 s/hotel × 2,916 hotels ≈ **37
hours of discovery** for a full Dubai prod run, before Phase 2 downloads a
single image. That is a rate-limit floor, not a bug — Agoda is paced at ≥1.5 s
and a hotel needs ~15-20 of its requests. Run it with the plan checkpoint and
expect to resume across sessions; do not expect a full city in an afternoon.

*Two hypotheses tested and FALSIFIED — recorded so nobody re-runs them:*
* **Not an idle timeout.** `wait_timeout` is 3600s, `max_user_connections` is
  unlimited, and a live idle test held the connection open successfully at 30,
  60, 120, 240 and 400 seconds. The drops are specific to that host's network
  path over a 16-hour run — which is *why* the fix is tolerance, not diagnosis.
* **Not a crossed environment.** `manifest.json` records
  `bookme_api_base: https://api.bookmesky.com` against `bookme_sky_prod`.
  Correctly paired.

**D-52 — The match early-break must be CITY-aware. Measured +25pp of recall.**
`match_hotel` stopped issuing queries as soon as ANY candidate scored
≥`NAME_STRONG`, regardless of city — so a global namesake suppressed the
disambiguating `"<name> <destination>"` query that would have found the real
hotel. Measured live against prod Agoda:

```
'royal plaza'        -> 5 candidates, 0 in Dubai, all scoring 100%
'royal plaza Dubai'  -> Royal Plaza Hotel Apartments, 100%, IN DUBAI
```

The pipeline was rejecting the namesake at 5,948 km and filing the hotel as
absent from Agoda, having never asked the question that would have found it.
Ranking had the same blindness: name-only sorting put `The Bristol Hotel`
(100%, 13,518 km) ahead of `The Bristol Hotel (formerly JW Marriott…)` (85%,
Dubai), so the right hotel could be ranked out of the `top_n` fetch budget.

Measured over 20 real `no_agoda_match` hotels: **3 → 8 found a Dubai
candidate**. Fixes: break only on a same-city strong match; filter to viable
candidates before the `top_n` slice, then rank same-city first. Costs zero
extra requests in the common case — a correct same-city match still stops
immediately; the extra query is issued only when the strong match is elsewhere,
which is exactly when it is needed.
Also closed here: a throttled *candidate fetch* used to fall through to
`no candidate above name threshold`. If every candidate we could afford to
check failed to fetch, the hotel was never assessed — that now returns
`unreachable`, not a verdict.

**D-53 — OUR OWN `size_sqft` migration was applied to UAT and never to prod.
The writer adapts, but the real fix is the migration.**
`size_sqft` is not a Bookme field and never was. It is room size taken from
**Agoda's** room data, which this project added a `v2_rooms` column for (UAT,
with operator approval) so it could be mapped into Bookme's schema. Prod never
received that DDL.
Verified 2026-08-20 against `bookme_sky_prod`: the column is absent, so every
statement naming it — the INSERT, the COALESCE backfill, the existing-rooms
read — is a hard `Unknown column` error. **Phase 2 would have failed on every
single hotel.** It stayed invisible only because the prod run was interrupted
during discovery and Phase 2 never ran; had the operator answered "y" at the
gate, all 821 hotels would have crashed.
**So there are two live options, and they are the operator's call:** apply the
same one-line migration to prod and size starts populating (94.3% of Agoda
rooms carry it), or leave prod without it and publish everything except size.
The adaptive writer below is what makes the second option safe today — it is
not an argument against the migration.
`db.room_columns()` reads the real schema once per process and the SQL is built
from it. Adapting is deliberate: size is a secondary display field, and
refusing to run an imagery pipeline over a missing optional column is the wrong
trade. Add the column to prod and it starts populating with no code change.
*Override if:* prod gains the column — nothing to change, it is detected.

**D-54 — Throttling is engineered THROUGH, not waited out. The sustained rate
adapts; the run never gives up.**
Operator direction, and the run's own evidence agrees: six consecutive
cooldowns each reported *"no successful call since cooldown #1"*. Sleeping did
not recover the block and could not have — the rate on the far side of each
sleep was the same rate that caused it. A sleep pauses the burst; it does not
change the behaviour that triggered it.

So a throttle now **raises the sustained interval first and sleeps second**
(1.5s floor → ×2 per cooldown → 15s ceiling), and that rate only eases back
after `RECOVER_AFTER = 40` consecutive successes — never on the first lucky
call after a block. Session identity (cookie jar **and** User-Agent, varied per
session, never mid-session) rotates every `AGODA_SICK_ROTATE` sick hotels: one
immutable UA across tens of thousands of calls is itself an automation
fingerprint, while a header that changes mid-session is a stronger one.

**The abort is deleted.** An unreachable hotel is now **requeued once** to the
end of discovery rather than consumed, so an outage costs *time*, not
*coverage*, and no hotel is written off on an answer we never got. Only a hotel
that fails the retry too is recorded. What remains is a reporting flag
(`agoda_degraded`, folder suffix `-AGODA-DEGRADED`) so a thin result is never
mistaken for a complete answer about a city.

**D-55 — Counters must count the question they are named for.**
`hotels_published_without_agoda` read **70** in a run that published **zero** —
it was counting attempts. Split into `hotels_agoda_blind_attempted` (Phase 1)
and `hotels_agoda_blind_done` (incremented at COMMIT). The gate summary
substitutes the Phase 1 counter alongside its Phase 1 `hotels_done`, because
pairing a Phase 1 total with a Phase 2 subtotal made the funnel over-count
(14 of 12) and flag itself `[MISMATCH]` — caught live, not in review.
Also fixed: `--slugs`/`--slugs-file` never bound `skipped_here`, so the manifest
write crashed with `UnboundLocalError` at the very end of a run, after all the
work was done.

**D-56 — PREVENTION over recovery: the Agoda property cache survives a
completed run. Measured 93% fewer requests.**
Everything in D-54 is *reactive* — it triggers once a block has already
happened. The largest lever for never tripping the limiter is simply to make
far fewer requests, and the pipeline was throwing that away.

`out/cache/agoda_*.json` is not run scaffolding, it is a **request cache with
its own expiry already built in**: `_cached_agoda` ages rooms out after
`CACHE_FRESH_DAYS`, while the date-invariant half (coordinates, isNHA, slug,
city id) is true until the building changes. The end-of-run sweep deleted it
wholesale, so every re-run of a city was a full re-scrape.

Measured live on 5 prod hotels, same code, cold vs warm:

| | requests/hotel | full Dubai (2,916) |
|---|---|---|
| cold cache | **14.6** | ~42,600 requests |
| warm cache | **1.0** | ~2,900 requests |

The sweep now keeps `agoda_*.json` and removes only the genuinely run-scoped
files (the plan, the Bookme probe checkpoint). Also worth knowing: the ladder
is **68%** of all Agoda requests (50 of 73 in the same measurement) — it is the
thing the cache is saving, and D-6a forbids shortening it, so caching is the
only way to not pay for it twice.
*Override if:* stale room lists ever cause a wrong publish — but note rooms
already expire on age and only a `ladder_complete` cache is trusted.

**D-57 — The browser fallback was an unmetered hole in the rate discipline.**
`agoda_browser.fetch_rooms` called `page.goto()` with no reference to the HTTP
pacer at all. A property page load is not one request — it pulls HTML, JS, XHR
and images — and the first production run made **89 of them**, on the same
address, while `agoda.py` was carefully spacing its own calls at 1.5 s. The
rate limiter saw all of it; our rate limiting saw none of it.
`_visit()` now charges the shared pacer `BROWSER_PACE_COST = 8` slots before
navigating (a deliberate under-estimate of a real page load — the point is that
it costs *something*), via `asyncio.to_thread` so the sleep delays our request
instead of stalling the browser's own I/O.

**D-58 — Pacing is jittered and duty-cycled, at a measured 24% cost.**
A run makes ~43,000 Agoda requests. Spacing every one at exactly 1.5 s is a
metronome — no browser produces a uniform inter-request gap for 17 hours, and
uniformity is trivially detectable. Every gap is now `interval × U(1.0, 1.45)`,
and the pacer takes a ~60 s break every 300 requests.

Both only ever ADD delay, so neither can make the client more aggressive than
the floor allows. `JITTER` is 1.45 rather than something larger on a measured
trade: what defeats a uniformity check is **variance, not a bigger mean**, and
the mean is what 43,000 requests pay for — 1.9 lifted the mean gap from 1.5 s
to 2.19 s (+46%, ≈ +8 h on a full city) to buy no more irregularity than 1.45
does at +24%.

**Honest cost of prevention, full Dubai, cold:**

| | pacing time |
|---|---|
| uniform 1.5 s (old) | ~17.9 h |
| jittered mean 1.86 s | ~22.0 h |
| + session breaks | **~24.3 h** |
| warm cache re-run | **~1.5 h** |

So prevention costs ~36% on a *first* run and saves ~92% on every run after it.
Set `SESSION_BREAK_EVERY = 0` to disable the breaks if a soak test ever shows
they buy nothing.

**D-59 — Did the two-phase split cause the throttling? Partly, and the fix is
not to undo it.**
Operator hypothesis, and it is mechanically sound: the old interleaved design
put image mirroring (COS/CDN traffic, not Agoda) between one hotel's Agoda
requests and the next's, so Agoda saw natural gaps. Phase 1 removed them —
same total requests, compressed into less wall clock, which is a **higher
sustained request density** even though `MIN_INTERVAL` never changed.
Estimated at ~20-35% denser, from mirroring costing roughly 10-30 s/hotel.

It is a contributing factor, not the primary cause — the first production run
still averaged ~43 s/hotel excluding cooldowns, close to the 46 s measured
post-fix. And the answer is **not** to re-interleave: the operator's own
reasoning for two phases holds (a failure mid-interleave leaves partial writes
scattered across hotels and rooms, which is exactly what would have been
unrecoverable in a run that failed this badly). D-58's session breaks
reintroduce the same gaps *deliberately and measurably*, instead of relying on
download time as an accidental rate limiter.

**D-60 — ONE Chrome for the whole run. The module had been warning against its
own behaviour.**
`agoda_browser.py`'s docstring has always said the browser is *"opened ONCE for
the whole batch"* because *"relaunching per hotel is both slow and exactly the
pattern bot-detection looks for."* It was not true: `fetch_rooms` opened and
closed its own browser inside `async with async_playwright()`, and `run.py`
called it with a **single-hotel list**. The first production run therefore
launched and tore down Chrome **89 separate times**. The module was describing
its own worst behaviour as though it were the design.

The justification in `run.py` — *"the price of publishing hotel by hotel so a
crash cannot lose a day's work"* — **expired at D-46**, when discovery and
commit were split. Discovery publishes nothing and check-points every hotel to
`plan.csv`, so a long-lived browser cannot lose work the checkpoint already
holds. Nobody revisited the comment when the premise under it changed.

Why the root cause was structural, not laziness: Playwright objects are bound
to the event loop that created them, and `asyncio.run()` **closes that loop on
every call** — so the browser could not have survived even if someone had kept
a reference. The fix is a single daemon-thread event loop for the process
lifetime (`_loop_thread`), with all browser work submitted to it
(`fetch_rooms_sync`), the page created once (`_session`) and reused.

Verified: three consecutive calls return the *same* page object, one browser is
launched, and `close()` leaves no stray processes. The live control-property
selftest still returns 6 rooms matching the HTTP path exactly, so the response
listener still works across a page that now outlives the call — it is removed
in a `finally`, or every later visit would stack another copy of it.

**The saving is not mainly the ~4 s per launch.** 89 brand-new Chrome instances
with empty profiles hitting one host is a far stronger automation signal than
one session browsing 89 pages; continuity is the point. `rotate()` is wired
into the Agoda breaker alongside the HTTP session rotation — rotating only the
HTTP identity would leave half our traffic still wearing the one that was
blocked.
*Override if:* a long run shows browser memory growth — recycle the page every
N visits rather than returning to per-hotel launches.

## Open / unsettled

- **O-1 — Is `/hotels/api/availability` a supported contract?** Undocumented,
  absent from dev-tools traffic (SSR-only). Governs whether the old path can be
  deleted permanently.
- **O-2 — Production yield is unmeasured.** Every coverage number on record is
  UAT, and UAT demonstrably under-reports (4/10 "unsellable" hotels sell on prod).
- **O-3 — Agoda date ladder stops at +12w.** Park Hyatt has 19 rooms at +34w.
  Measured benefit of extending: ~1/18 properties. Worth doing, not a step change.
- **O-4 — No unique constraint on `v2_rooms(v2_common_hotel_id, name)`**, so
  duplicate protection is a check-then-act across processes. Adding it requires
  schema approval.
