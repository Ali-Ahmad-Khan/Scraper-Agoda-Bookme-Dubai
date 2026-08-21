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

**D-61 — PRODUCTION PROOF, run `20260820-161210`: the throttle was engineered
through, live, exactly as designed.**
100 Dubai hotels on prod, `--plan-only`. Agoda blocked mid-run and the pipeline
worked through it without losing a single hotel. The sequence, from the log:

```
[68/100] pearl residence ...   agoda unreachable (HTTP 502) -- requeued
[69/101] arabian park ...      agoda unreachable (HTTP 502) -- requeued
  agoda throttled 6x in a row -- cooldown #1: sustained pace 1.5s -> 3.0s
[70/102] metropolitan ...      agoda unreachable (HTTP 502) -- requeued
  !! AGODA UNREACHABLE for 3 hotels in a row. Rotated session identity;
     sustained pace now 3.0s. These hotels are requeued, not written off
  agoda steady for 40 calls -- easing pace to 2.0s
  agoda steady for 40 calls -- easing pace to 1.5s
[101/103] pearl residence ...  -> booking takes over for 4 room(s)
[102/103] arabian park ...     -> mapped 49 rooms (48 with candidate photo)
[103/103] metropolitan ...     -> mapped 47 rooms (39 with candidate photo)
```

Every element of D-54 fired in order and each did its job: **requeue** (no
unearned zero), **raise the rate before sleeping**, **rotate identity at 3**,
then **ease back only after 40 clean calls** — twice, 3.0 → 2.0 → 1.5 s. Full
recovery inside one cooldown, against six consecutive failed cooldowns and
3.75 hours of dead sleep in the previous run.

**The requeue alone saved 96 rooms.** `arabian park` (49) and `metropolitan`
(47) both succeeded on the retry; under the old code both would have been filed
as `no_agoda_match` and lost.

| | 20260819 (before) | 20260820 (after) |
|---|---|---|
| hotels mapped | **0** of 821 | **86** of 100 |
| rooms with a photo | 0 | **2,528 of 2,965 (85%)** |
| MySQL drops / deferrals | 221 / 221 | **0 / 0** |
| errors | 0 (nothing got far enough) | **0** |
| booking rescues | 0 | **677 rooms across 29 hotels** |
| existing-room queries | 2,916 | **1** |
| throttle outcome | 6 cooldowns, never recovered | **1 cooldown, fully recovered** |
| duration | 15.9 h → nothing | 110.6 min for 103 |

⚠️ Still unmeasured after this run: **the browser fallback recovers almost
nothing in either mode** — 3 of 26 here (11.5%) against 8 of 89 (9%) in the
previous run. The module exists on the premise that a browser rescues roughly a
third of properties. Two runs now say ~10%. That premise needs re-testing on
its own; see O-5.

**D-62 — `size_sqft` cannot be added by the pipeline's own account, and that is
correct.**
`GRANT SELECT, INSERT, UPDATE, CREATE, INDEX, EXECUTE ON bookme_sky_prod.*` —
**no ALTER, no DELETE, no DROP.** The migration fails with 1142 by design, so
prod is structurally incapable of losing data to this pipeline regardless of any
application-level guard.
`add_size_sqft_column.py` detects 1142 and prints the statement for whoever
holds DDL rights instead of failing obscurely:

```sql
ALTER TABLE v2_rooms ADD COLUMN size_sqft INT NULL, ALGORITHM=INSTANT;
```

Appended at the end (not `AFTER thumbnail`, which can force a full table
rebuild) with `ALGORITHM=INSTANT` stated so the server refuses loudly rather
than silently taking a lock. Until a DBA runs it, `db.room_columns()` omits
size from every statement and everything else publishes normally (D-53).

**D-63 — A deterministic hook makes DELETE impossible from this session.**
`.claude/no-destructive-sql.sh`, wired as a `PreToolUse` hook on Bash in
`.claude/settings.json`. Blocks `DELETE FROM`, `DROP TABLE/DATABASE/COLUMN`,
`TRUNCATE`, and any invocation of `cleanup_for_fresh_run.py` (which genuinely
deletes rows and was written for UAT). Permits `ALTER TABLE … ADD COLUMN` only.
Exit code 2 means the command never executes — this is not guidance a model can
reason past. Verified live by tripping it.
Three independent layers now stand between this project and data loss: the
MySQL grants (no DELETE/DROP at all), `db.py::_sql()`'s additive-only guard,
and this hook.

**D-64 — MEASURED: persistent vs per-hotel browser is a wash. My stated
justification for the change does not survive the measurement.**
A/B against live Agoda, 6 resolved properties, same set both arms, **ABBA
ordering** so a block building over time could not be blamed on whichever arm
ran second:

| | persistent | per-hotel |
|---|---|---|
| rooms recovered | **19** | **19** |
| properties at zero | 3/6 | 3/6 |
| wall clock | 136 s | 138 s |

Identical recovery, and — the part that undercuts my own argument — **no
meaningful speed difference either.** I justified the change partly on saving
~4 s per hotel; that saving is swallowed by the pacer charge, since a browser
visit is billed 8 slots (~15 s) in *both* modes. The launch cost is hidden
inside pacing that both arms pay.

So of the two claims made about this change, one is now falsified (speed) and
the other remains unmeasured (block resistance). **This A/B measures behaviour
under HEALTHY conditions — it cannot answer "which gets blocked sooner under
sustained load", which is the question that actually matters.** Settling that
needs a long soak test deliberately pushed until it breaks, which is not
something to do casually against infrastructure this project depends on.

**Recommendation on the evidence: default `AGODA_BROWSER_PERSIST = False`** —
back to per-hotel. Not because per-hotel is proven better, but because a change
with no demonstrated benefit should not carry the extra failure surface
persistence adds (a wedged browser poisoning later fallbacks, memory growth
over a 2,916-hotel run). The persistent path stays behind the flag, working and
selftested, for whoever runs the soak test.
*Override if:* a soak test shows one mode survives materially longer.

**Flipped 2026-08-20, on operator instruction, after the measurement above.**
`config.AGODA_BROWSER_PERSIST = False` is now the live default.

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
- **O-5 — RE-MEASURED, and the picture got worse, not better. Recommend
  disabling the browser fallback; not yet done.**
  Third measurement, live: for 8 HTTP-empty Dubai properties, ran (a) the
  browser alone, (b) the 10-rung escalation ladder alone, (c) the ladder
  unioned with whatever the browser found — because `agoda_rooms()` always
  runs the ladder regardless of browser outcome, so the question that matters
  is MARGINAL value, not raw rescue rate.

  **The browser recovered 0 rooms in all 8 properties — 0/8, not a partial
  rescue.** The test's own auto-verdict claimed "+2 marginal rooms" at Shangri-La,
  but that is a **measurement artifact, not a browser contribution**: the browser
  column for that row is also 0, so the 15-vs-17 gap is two independent live
  calls to the same 10-rung ladder, seconds apart, returning different counts —
  real-time inventory movement, not anything the browser found. Correcting for
  that: **true marginal contribution across this batch was 0 rooms**, at a cost
  of 192 s (24 s/property).

  Now three independent measurements, all pointing the same direction against
  the module's original "roughly a third" premise:

  | measurement | rescue rate |
  |---|---|
  | prod run 1, fresh browser/hotel | 8/89 = 9% |
  | prod run 2, persistent browser | 3/26 = 11.5% |
  | marginal-over-ladder, live | **0/8 = 0%** (raw rescue was also 0/8) |

  **Not disabled yet** — this is a recommendation, not an action taken, because
  it removes a whole rescue path and the sample sizes (89, 26, 8) are not huge.
  If confirmed on a larger batch, the honest move is to retire
  `agoda_browser.py` for cost (24 s/property, an extra full Chrome page load =
  more detection surface) with near-zero demonstrated benefit. Operator
  decision.

---

**D-65 — The isNHA veto was rejecting rentals that ARE the Bookme row, not just
rentals that name-drop a hotel. 430 of 875 `no_agoda_match` hotels died on that
one line; 127 of them had a candidate whose name was the Bookme name.**

Run `20260820-061304` (1,500 Dubai hotels, 532 mapped = 35%) reported 875
`no_agoda_match`. Bucketed by cause, that number is not one failure:

| cause | hotels | verdict |
|---|---|---|
| isNHA veto | 430 | **partly wrong — fixed here** |
| no candidate above name threshold | 282 | genuine, see D-66 |
| scored rejection (distance/city/strict) | 154 | mostly correct |
| Agoda returned no suggestion at all | 9 | genuine |

`match_hotel`'s isNHA veto exists for a real case (D-?/`run.py` docstring): a
vacation rental whose title embeds a hotel's name — "Luxury Burj View 2BR in
Kempinski Central Avenue" — sits in the same building as the Kempinski, scores
100% on name, and is not the hotel. Neither name nor distance can rule it out,
so the flag had to.

What the veto missed is that **Dubai's Bookme catalogue is itself full of
vacation rentals.** For those rows the correct Agoda counterpart IS an isNHA
listing, and the blanket veto made them permanently unmatchable. The operator
caught this from two examples; the data says it is 430.

- `nasma luxury stays central park tower` → rejected `Nasma Luxury Stays - Central Park Tower`
- `nasma luxury stays limestone house` → rejected `Nasma Luxury Stays - Limestone House`
- `kennedy towers cayan tower` → rejected `Kennedy Towers - Cayan Tower [Dubai]`

**The separator is `strict_score`, and it is clean.** `token_set_ratio` scores
BOTH the name-dropping case and the same-listing case at 100 and cannot tell
them apart. `token_sort_ratio` is length-sensitive and does — measured over all
430:

| population | strict_score |
|---|---|
| name-drops a hotel (the case the veto is for) | **≤ 59** |
| genuine same-listing pair | **90–100** |

102 of the 430 scored a flat 100 — verbatim-identical names. The gap between 59
and 90 is wide enough that the threshold choice is not delicate.

**A high score alone is NOT sufficient, and this is the part that would have
shipped a precision regression.** Same-operator siblings differ by exactly one
token — a unit number:

- `frank porter rimal 3` vs `Frank Porter - Rimal 1` → strict **95.0**
- `frank porter goldcrest views` vs `Frank Porter - Goldcrest Views 2` → **96.6**
- `frank porter fairmont residences` vs `Frank Porter - Fairmont Residence South` → **92.8**

All three would have passed a pure score gate and published the wrong
apartment's photos. So numerals and compass words are vetoed on disagreement
rather than scored — the same treatment `CLASSES` already gives room class,
for the same reason. `match.unit_marks` / `match.same_listing`.

The destination is dropped before scoring: Agoda appends "[Dubai]" where Bookme
does not, and since the whole comparison is already city-scoped that token is
pure length noise. It moved three `Kennedy Towers - X [Dubai]` listings from
88–90 to a flat 100 and cost nothing (127 rescued vs 124, zero lost).

**Measured effect on matching:** 430 isNHA rejections → **127 rescued** (30%).
`hotels_no_agoda_match` 875 → 748.

**Verified live, and the match quality is not in doubt.** Sampled 12 of the 127
end-to-end against Agoda: **12/12 matched, every one at `conf=high`, coordinates
1–64 m apart.** Two of them are the unit-mark veto working as designed —
`frank porter beach vista tower 2` matched `...Beach Vista Tower 2` at 6 m and
`frank porter - reehan 5` matched `...Reehan 5` at 64 m, while the same rule
blocks `Tower 1` against `Tower 2` and `Rimal 3` against `Rimal 1`.

**But do NOT read 127 as +127 published hotels, and an earlier draft of this
entry did exactly that.** Two measurements cut it down:

- **97% of the unmatched hotels have no Bookme rooms either.** Of 614
  `no_agoda_match` lines in the run log, 597 read "no agoda match, **no bookme
  rooms either**". For those, a match only pays if Agoda supplies inventory to
  CREATE rooms from — there is nothing on the Bookme side to attach photos to.
- **Most of these rentals have no Agoda inventory.** In the live sample, 10 of
  12 returned `supplier_count=0` — Agoda affirmatively reporting that no
  supplier offers the property. Only **2 of 12 (17%)** yielded rooms, 4 in total.

So the honest projection is **~17–25% of 127 ≈ 20–30 hotels** gaining
publishable rooms, i.e. `hotels_mapped` 532 → **roughly 550–560 of 1,500
(35% → ~37%)**, not the 659/44% that the match count alone suggests.

The other ~100 are still worth rescuing, just for less: they are reclassified
from "absent from Agoda" (a permanent-looking, wrong fact) to "on Agoda, no
inventory" (`hotels_no_rooms`, revisitable when inventory returns), they enter
the property cache so future runs cost 1 request instead of 14.6, and a
matched hotel is eligible for Booking.com gap-fill where a written-off one is
not.

---

**D-66 — The 282 "no candidate above name threshold" hotels are largely a
genuine dead end, and lowering the threshold would be a precision disaster.
Measured, and the measurement says DON'T.**

The tempting read of 282 hotels is "our floor is too high." Tested live against
Agoda's suggest endpoint on a 40-hotel random sample of that exact bucket:

- For real, well-known hotels — `jumeirah creekside dubai`, `dusitd2 kenz hotel
  dubai` — Agoda's suggest index returns **zero candidates**, under the plain
  name, the name + destination, AND the address query. Not a threshold problem.
  The property is not reachable through that index.
- Where a sub-threshold Dubai candidate DOES exist, it is almost always a
  different property in the same building. Of 16 such candidates, ~2 were
  correct:
  - `key view dec tower 2` → `Frank Porter - Dec Towers 1` ✗
  - `ac pearl holiday marina skyline view` → `Rove Dubai Marina` ✗
  - `the dubai holiday home, spacious 2 bed` → `The Dubai EDITION` ✗
  - `time opal hotel apartments` → `TIME Oak Hotel & Suites` ✗ (different TIME hotel)
  - `suha creek hotel apartment` → `SUHA Creek, Waterfront Al Jaddaf` ✓ (rebrand)

**Lowering `NAME_OK` from 72 would admit ~87% garbage**, so it stays at 72.

Distance does not rescue the good ones either, and this is worth recording
because it is the obvious next idea: `_confidence` could admit a sub-72 name
when coordinates prove the building. It cannot — Dubai Marina packs dozens of
towers inside `NEAR_KM`, and the run's own data shows `sea view 2bd in new
52|42 tower` sitting 0.36 km from `Rove Dubai Marina`. Same 350 m, different
buildings.

Honest conclusion: this bucket is Agoda's index coverage, not our tuning. It is
the part of the 875 that is genuinely **not in our hands**.

---

**D-67 — `--limit N` now means a fixed window of the catalogue, so runs are
reproducible. It previously meant "N hotels not already done", which slid.**

Operator-reported, and correct. `db.hotels()` filtered `skip_ids` BEFORE
counting toward the limit, so `--limit 1500` meant "walk the catalogue until
1,500 hotels that aren't in the ledger have been collected." As the ledger
fills, the window slides further down the catalogue on every re-run: no two
runs cover the same hotels, and no result can be reproduced or backtracked to
the run that produced it.

Now the limit slices the ordered rows first and `skip_ids` subtracts within
that window. `--limit 1500` is always the same 1,500 hotels; a run that skips
200 fresh ones processes 1,300, and those 1,300 are a subset of the same fixed
1,500. Costs nothing — it did not bite this run (`hotels_in_ledger_globally: 15`,
`hotels_skipped_fresh: 0`) but would have on every subsequent one.

---

**D-68 — Proactive identity rotation, on the session break we already have —
NOT on a block threshold discovered by provoking Agoda.**

The operator asked for rotation at a fixed threshold rather than only on a 502,
and asked that the threshold be measured rather than assumed. Two findings:

**Finding 1 — the pacer already had the boundary, and was wasting it.** Every
`SESSION_BREAK_EVERY = 300` requests `_pace()` sleeps 60 s, then carries on
with *the same cookie jar and the same User-Agent*. A 16-hour run therefore
presented as one continuous session with suspicious 60-second gaps in it.
Rotating identity across that gap is free: no extra requests, no extra time.

**Finding 2 — the threshold cannot honestly be measured, and does not need to
be.** Locating "the point where Agoda blocks" means deliberately getting
blocked on production infrastructure this pipeline depends on, repeatedly, to
find an edge Agoda can move whenever it likes. That is a bad trade and it is
not the only evidence available. Run `20260820-061304` completed **1,500 hotels
over ~16.2 hours with zero throttling events** — no cooldowns, no rotations, no
requeues anywhere in the log — at `SESSION_BREAK_EVERY = 300`. So 300 requests
is already *demonstrated* to sit inside Agoda's tolerance. Rotating on a
cadence we know is safe beats guessing at one we would have to break things to
learn.

Jar and header move **together**, never one without the other. `UA_POOL`'s own
note is the reason: a new User-Agent on an established cookie jar is a browser
that changed identity mid-session, which is a *stronger* automation signal than
never rotating at all. Clearing the jar is what makes the new header coherent.

---

**D-69 — "Accessible" means two unrelated things, and the room matcher was
reading both as disability. Resolved by grammar, not by a list of facilities.**

31% of the run's review file (58 of 188 rows) turned on accessibility
vocabulary, every one of them pinned at exactly 74.0 — which is not a score at
all but `ROOM_ACCEPT_CEILING`, the deliberate safety cap from the ACCESSIBILITY
rationale. Two of the three causes were outright bugs:

**(a) Entitlement misread as disability (15 rows).** "1 King Premium Club
Lounge **Accessible**" is a club-lounge perk. It was being compared against
Bookme's "1 King Bed Premium Room Club **Access**" — the identical perk — and
scored as an accessibility *disagreement*.

**(b) Disability vocabulary the veto did not know.** "Superior **Special
Needs** Room" vs "Superior **Accessible** Single Room" are both accessible
rooms. Worse, `PROMO_NOISE` strips "special" as advertising language, so by the
time `features()` looked, the surviving token was a bare "needs" and the
accessibility signal was **gone entirely**.

**The first attempt at (a) was wrong and was rejected in review.** It special-cased
`club|lounge` — patching the instance that surfaced. Hospitality grants access
to an open class of things: beach, pool, spa, gym, terrace, garden, rooftop,
marina, ski, shuttle. The production data already contained `beach access`. A
list is wrong the moment a property invents an amenity.

Read off the grammar instead — the same argument `view_of()` already makes for
view subjects:

- **"access" is a noun** and takes a subject: `<something> access` is access TO
  that something, whatever it is. No list, no ceiling. The disability reading is
  written `<disability word> access` and is caught before this.
- **"accessible" is an adjective.** Default: disability. Entitlement only when
  preceded by `PERK_NOISE` — the module's *existing* service-entitlement
  vocabulary ("Club **Lounge** Accessible").

**Ambiguity resolves toward disability, deliberately.** An intermediate version
read "any unrecognised preceding word" as a facility subject, which the existing
suite caught immediately: "**Cosy** Accessible Room" became an entitlement,
silently losing a real disability marker, because "cosy" is one of an open class
of adjectives. The asymmetry is the whole point — a wrong "accessible" costs one
pair held in the review band where a human still sees it; a wrong entitlement
auto-publishes a standard bathroom to the guest who most needs a roll-in shower.

**Effect:** 14 review rows freed from the ceiling to score on merit. Verified
against 35 phrasings, including facilities the code never names.

**(c) NOT changed: the 45 genuinely one-sided rows.** The operator's reading is
that "accessible" is an amenity that does not change how the room looks, so
`Superior Room (Accessible)` should take `Superior Double Room`'s photos. That
is a product call and it is theirs to make, but the cap is not costing them the
match: a capped pair lands in `rooms_review.csv` **with the candidate image and
a `decision` column**, and `apply_review_decisions()` publishes whatever a human
approves. The recovery path already exists and needs no code change. Removing
the cap instead would auto-publish standard-bathroom photos onto accessible
rooms across every future city, unreviewed. Recommend keeping it and approving
the 45 in the review file. Operator decision.

---

**O-5 UPDATE — the browser fallback is NOT dead weight after all. Correcting my
own recommendation.**

The entry above recommended retiring `agoda_browser.py` on 0/8 marginal value.
Run `20260820-061304`'s log refutes the strong form of that: over the ~727
hotels the captured log covers, the browser was the room source for **2 hotels**
— `al jazeera hotel apartments llc` (1 room) and `premier inn dubai dragon mart`
(6 rooms → 12 mapped). Small, but not zero, and my 8-property sample was simply
too small to see a ~0.3% event.

Retirement is **off the table** on current evidence. The measured cost
(24 s/property, one extra Chrome page load of detection surface) is real, so the
open question narrows to whether the fallback should be *gated* — tried only for
properties whose HTTP grid is empty AND whose `supplier_count > 0`, which is
Agoda's own signal that an empty grid is anomalous rather than truthful. That
gate is not built. Still an open question, no longer a retirement recommendation.

---

**D-70 — MEASURED: the rentals we "lose" to the isNHA veto have no Bookme
inventory to lose. The veto is not the bottleneck it looks like.**

The operator's read of run `20260820-061304` was that Dubai's catalogue is full
of rental listings and the isNHA veto is throwing them away. D-65 already
rescued 127 of the 430 rejections (names that ARE the Bookme row). The question
was what to do about the remaining 303.

Before tuning the threshold, I asked what those 303 rows actually are. Probing
the Bookme availability API directly:

| population | n sampled | have ANY sellable rooms |
|---|---|---|
| whole Dubai catalogue (baseline) | 30 | 26.7% |
| isNHA rejections — low band (<60) | 40 | **0%** |
| isNHA rejections — mid band (60–90) | 40 | **0%** |
| isNHA rejections — high band (D-65 rescued) | 40 | 7.5% |
| `no_agoda_match` (all reasons) | 30 | **0%** |
| `no_rooms_any_date` | 30 | **0%** |

**Controlled twice, because a 0% result is exactly the shape a broken probe
makes.** Hotels known to have produced rooms in the run returned 3–38 rooms on
the same single-shape call (11/12 nonzero), so the probe works. And 12
NHA-rejected slugs re-probed across **7 date shapes** (the pipeline's own
spread: 1–12 weeks out, 1–2 adults, 1–3 nights) returned 0 rooms on every
shape — so this is not a seasonal or single-date artifact.

**These are dead catalogue rows** — `v2_common_hotels` entries Bookme does not
sell. Accepting their Agoda rental match would create room records (Agoda
leftovers DO publish as new rooms, `map_rooms` final loop) for properties with
no sellable inventory.

**Three candidate discriminators tested and all falsified:**
- `v2_hotels.hotel_type_id` / `hotel_category_id` — the columns exist, and are
  **100% NULL** across all 3135 Dubai rows. Bookme does not classify these.
- Agoda's own `accommodation_type` — "Apartment/Flat" dominates *every* band
  (122/154/105). No separation.
- Bookme room-set corroboration — unavailable: the population has no rooms.

**Not changed, and why.** The mid band is genuinely ~50/50 by inspection —
`driven holiday homes - 29 boulevard` → `Driven Holiday Homes Apartment in 29
Boulevard` is the same listing; `frank porter - ag tower` → `Frank Porter -
Sparkle Tower 1` is a different building from the same operator. No available
signal separates them, so auto-accepting would attach rental photos to the
wrong building about half the time. The threshold stays at 90.

**What DID change:** the rejection reason now carries the Agoda property id and
URL, so the 303 are adjudicable by a human instead of having to be re-derived.

**The open product question, which is the operator's and not the pipeline's:**
should Bookme hold room records for properties it does not sell? If yes, the
mid band becomes worth a supervised pass. If no, these 303 are correctly
dropped and the isNHA veto is doing its job.

---

**D-71 — "no listing on any platform" was reporting a catalogue fact as a
matching failure.**

Run `20260820-061304` read as *532/1500 published (35%)*, with 801 hotels under
"no listing on any platform". That framing invites the question the operator
asked: *is it really that 800 hotels had no counterpart?*

The 801 are precisely the hotels that took the `if not bm_rooms: continue`
branch — **no Agoda match AND no Bookme rooms**. That is not one fact, it is
two, and they have different remedies: "Agoda has no listing" is a matching
outcome a better matcher could improve; "Bookme sells no rooms here" is a
property of our own catalogue that no matcher can touch.

Added `hotels_no_bookme_rooms`, counted at that branch and reported as a
sub-row. Replaying run 2's own numbers through the new summary:

```
  not published                968/1500
    ├─ no listing on any platform 801
    │   └─ and no bookme rooms either 801      <- 100% overlap
    ├─ listed but zero rooms   167
```

**The entire shortfall is properties with nothing to map.** Deliberately NOT
expressed as a "coverage of mappable stock" percentage: every hotel with
something to map does get mapped, so that ratio is 100% by construction — a
tautology dressed as a metric. The raw count is the honest form.

---

**D-72 — MEASURED: Booking cannot cover the hotels Agoda misses. The obvious
fallback has no yield.**

Two separate reasons, one by design and one by measurement.

**By design:** Booking is gap-fill-only — it fills rooms Bookme already sells
and never adds new ones. `_booking_fill`'s docstring already carries the
reasoning: a Booking listing may legitimately be *one apartment inside the
building*, so its room set is not the hotel's. *"Filling a room Bookme already
sells cannot introduce a room that does not exist; adding one can."* That is
why the `not bm_rooms` branch skips Booking rather than an oversight.

**By measurement:** it would not have helped anyway. `resolve_verified` against
live Booking.com:

| population | resolved |
|---|---|
| hotels the run processed normally (control) | **4/8** |
| hotels Agoda could not match | **0/10** |

**This result required two attempts and the first was wrong.** My initial run
passed `"ae"` as the `city` argument and returned 0/14 — which looked like a
clean finding until the control *also* returned 0/12, including Shangri-La and
Jumeirah Emirates Towers, which are certainly on Booking. The zero was my bug,
not Booking's. Re-run with `city="Dubai"` the control resolves and the test
still does not.

Individually-listed Dubai rentals are not findable on Booking under the Bookme
row's name at ≤250 m. Building an agoda-blind Booking path would add a whole
source-of-truth branch for approximately zero hotels. **Not built.**

**A flag I raised and then withdrew.** The control produced `swissotel al
murooj` → `roda-al-murooj-hotel`, which I recorded as a plausible
cross-property match. It is not. Scored offline: `swissotel al murooj` vs
`roda al murooj` is **78.3**, well under the `BOOKING_MIN_NAME = 90` gate, so
it could not have passed on that name. The gate scores the candidate's
**TITLE, not its slug** — so Booking's title must itself read "Swissotel Al
Murooj", and the slug is simply stale from a rebrand. That is the same
phenomenon `resolve_verified`'s own docstring already records (`hilton dubai
the walk` → `hilton-dubai-jumeirah-residence`, "the SAME hotel under an old
slug"). **No defect, and `BOOKING_MIN_NAME` needs no change on this evidence.**
Recorded because a withdrawn flag is worth as much as a raised one — the next
reader would otherwise re-open it.

---

**D-73 — Generic room images: nothing to fix, and "fixing" it would be a
regression.**

The operator observed rooms occasionally receiving generic-looking images and
asked whether property-level photos should be used where room-level ones are
missing. **Both sources are already room-level by construction:**

- Booking's page carries `allRoomPhotos` (each with `associated_rooms`) and
  `hotelPhotos` **separately**. This module reads only the first. Hotel-level
  photos are structurally excluded — that binding is the entire reason Booking
  is in this pipeline.
- Agoda's images come from the `masterRoom` grid, where each entry carries its
  own image list. Scraping the DOM instead bleeds images between rooms, which
  is why it is not done that way.

So a generic-looking photo is the *property's own* room photo — a hotel that
uses one stock shot across several room types. Adding a property-level fallback
would put lobby and pool photos on rooms, unreviewed, across every future city.
The correct behaviour for a room with no room-level photo is the current one:
publish it without an image and report it (`rooms_without_candidate_images`).
**No change. Handling this would trade a known-correct 80% for a
plausible-looking 100%.**

---

**O-6 (OPEN, proposal only — nothing built) — Proxy rotation and concurrency:
where the time actually goes, and what it would cost to buy it back.**

Asked by the operator: we rotate user-agents at best — could we rotate proxies,
and would that allow concurrency?

**First, where the 16.2 h of run `20260820-061304` actually went.** Derived from
its own manifest, not estimated:

| stage | time | concurrent? |
|---|---|---|
| Bookme harvest (12 base + 16 escalation shapes) | 4 920 s (1.4 h) | **yes** — 8 workers |
| Agoda matching + Booking gap-fill, 1500 hotels | 53 445 s (14.8 h) | **no** — strictly serial |

That is **35.6 s per hotel**, serial. At the measured ~14.6 Agoda requests per
cold hotel and a mean jittered gap of 1.86 s (`MIN_INTERVAL` 1.5 × D-58's
measured +24%), **~27 s of those 35.6 s is the pacer deliberately waiting** —
roughly **76% of wall clock is self-imposed delay**, not work.

So the operator's instinct is right: this is the lever, and it is a big one.

**Why concurrency alone (no proxies) would be a regression.** The pacer is not
arbitrary politeness — it exists because Agoda throttles per source IP. Running
N threads from one IP does not get N× throughput; it reaches the block N× sooner
and then everything stalls behind the same cooldown. D-59 already recorded a
weaker version of this: merely removing the download pauses between requests
(the two-phase split) made the stream denser and contributed to throttling.
Threads would be that effect multiplied. **Concurrency is only unlocked by
having more than one identity to be concurrent from.**

**What proxy rotation would actually buy, and cost.**

- *Free / public proxy lists* — not viable. Already-burned IPs, and an untrusted
  MITM on a session that carries our Bookme credentials. Not a cost question.
- *Datacenter proxies* (~$1/IP/month, unmetered) — cheap, and likely useless.
  Agoda's edge blocks known datacenter ASNs aggressively; this needs a
  10-IP trial before any spend, not a purchase decision.
- *Residential/mobile pools* (Bright Data, Oxylabs, Smartproxy — ~$3–8/GB) —
  would work, and would give close to linear speedup: N pools → N parallel
  Agoda streams, each with its own pace budget and its own UA/cookie identity
  (the rotation machinery for that already exists in `agoda.session()`).
  **The cost is metered by bytes, and this pipeline is byte-heavy**: the
  property payload is 803 KB to reach a 27-byte verdict, ×14.6 requests
  ≈ 11.7 MB per cold hotel. A full cold Dubai run (2916 hotels) is
  **~34 GB ≈ $100–270**. A warm re-run (1.0 req/hotel, D-56) is ~2.3 GB ≈ $7–19.

**Two cheaper levers that should be tried FIRST, because they are free and they
also reduce any future proxy bill:**

1. **Cut requests per hotel** (attacks *time*). `top_n = 5` means up to five
   803 KB property fetches per hotel, and the ranking already puts same-city
   candidates first (D-52).

   **CORRECTION — the "~40%" first written here was wrong.** That was 2/5, the
   share of *candidate slots* removed, mislabelled as a share of total pacing
   time. The real arithmetic: dropping `top_n` 5 → 3 removes at most **2 of the
   ~14.6 requests a cold hotel makes**, so the ceiling is **~14%**, not 40%.

   And ~14% is an upper bound that will not be reached, because the candidate
   loop **breaks early on a strong same-city match** — slots 4 and 5 are only
   ever fetched for hotels where the first three all failed, which are largely
   the hotels that end up unmatched anyway. So the saving is real but modest,
   and it is bought with recall.

   **WITHDRAWN 2026-08-21, on operator instruction — `top_n` stays at 5.** The
   operator's objection is the correct one and outranks the saving: a lower
   `top_n` risks never fetching a candidate that would have scored higher than
   the ones examined. That is a recall loss traded for ~14% of a wait, on a
   pipeline whose entire value is finding the right property.

   Worth recording *why* the objection lands so cleanly: `match_hotel` examines
   **every one** of the top_n candidates — there is no early break, all paths
   `continue`, and the winner is the running best by `(distance, -name_score)`.
   That break existed once and was removed for this exact reason (see the
   comment at the `viable`/`ranked` filter). Narrowing the slice would
   reintroduce, at the ranking boundary, the very failure the break's removal
   fixed inside the loop. Lever (2) below is unaffected and still stands.
2. **Cut bytes per request** (attacks *proxy cost*, not time). 803 KB for
   `is_nha`, lat, lon and city_id is the pipeline's worst payload-to-value
   ratio anywhere. If a lighter endpoint carries the same four fields, the
   proxy bill drops ~10× and the cache gets cheaper too.

**Recommendation:** do (1) first — it is free, offline-measurable, and attacks
the actual 76%. Treat residential proxies as a real and justified option after
that, but scoped: a 10-IP trial measuring block rate and effective parallelism
before committing to a metered plan. **Do not buy datacenter IPs without the
trial.** Nothing here is built or committed.

---

**D-74 — Audit of every label in the run summary. Four said something other
than what their counter holds; one said the opposite.**

Prompted by the operator catching a real defect in D-71's own presentation: the
sub-row "and no bookme rooms either" was hung under the 801 bucket, which reads
as if the 167 in "listed but zero rooms" were a *different* kind of failure. It
is not. **Both buckets require `not bm_rooms`** — the 801 via the
no-agoda-match continue, the 167 via `not ag_rooms and not bm_rooms`. They
differ only in whether Agoda could identify the property.

That prompted a line-by-line audit of the whole summary. Every row was checked
against the counter behind it, not against what it sounded like:

| label | what the counter actually holds | fix |
|---|---|---|
| `no listing on any platform` | **Only Agoda was asked.** These hotels take the `not bm_rooms` continue, which fires *before* the Booking takeover — Booking never sees them. | `agoda found no listing` |
| `listed but zero rooms` | Same zero-Bookme-rooms condition as the 801, not a separate class | `agoda listed it, no rooms either platform`, + one shared `(info)` line across both |
| `bookme had nothing live` | Hotels the harvest never got an **answer** for — an open question, not a stated zero. Read **0** while 968 hotels genuinely had no rooms: the label said the opposite of the truth. | `bookme never answered` |
| `published` (at the gate) | Nothing is published at the gate — no image fetched, no row written. A `--plan-only` run read exactly like a completed one. | `mapped (nothing published yet)`, and `not published` → `not mapped` to match |
| `ROOMS · N listings / 532 published hotels` | same | `across 532 hotels` |

**The inventory fact is now stated once, across both buckets, and excludes
errors** — a hotel that crashed may well have had rooms, so folding it in would
reintroduce the same category error in the other direction:

```
  (info) no sellable bookme rooms 968/1500 (65%)  -- BOTH rows above
```

**Standing rule this establishes:** a summary label is part of the contract, not
decoration. When a counter's name and its increment site disagree, the label is
the bug — it is what a reader acts on, and unlike a wrong number it never trips
the `[MISMATCH]` check. `hotels_unresolved_on_bookme` reading a confident `0`
through a run where 65% of the window had no inventory is the case in point.

**Follow-up, same session — vague source words are the same bug.** The operator
then asked what "either platform" referred to, which was a fair hit: the three
sources are consulted in *different combinations per branch*, so no collective
noun is readable.

- `no agoda listing; bookme had 0 rooms` — asked **Agoda only**. Booking is
  never reached: the `not bm_rooms` continue fires first, and Booking only fills
  rooms Bookme already sells. The row now says so inline, because "why wasn't
  Booking tried here" is a question this file has now been asked twice.
- `agoda listed it; 0 rooms on agoda AND bookme` — condition is literally
  `not ag_rooms and not bm_rooms`. Two sources, both named.
- `(info) bookme itself returned 0 rooms` — Bookme, across every probed shape.
- Per-hotel log line "no rooms on either platform" → "no rooms on agoda or
  bookme".

**Rule:** name the sources a branch actually queried. Never "either", "any", or
"both" platform — the reader cannot tell which two of three were consulted, and
in this pipeline it is a different pair on every branch.

No counter's *value* changed across either pass. Nine labels and one placement did.

---

**D-75 — MEASURED: the Booking takeover almost never finds photographs, and
`hotels_mapped` was counting hotels that got none.**

The operator asked what actually happens when Agoda finds no match but the
hotel *does* have Bookme rooms — does Booking cover it? That path exists
(D-37's takeover) and fires correctly. Measured live on the first 106 hotels of
run `20260821-122343`:

| | hotels | rooms planned | rooms with a photo | hotels with **zero** photos |
|---|---|---|---|---|
| agoda identified the hotel | 84 | 3281 | 2916 (**88.9%**) | 3 |
| agoda-blind (booking took over) | 8 | 243 | 93 (**38.3%**) | **7 of 8** |

**All 93 of those photos came from one hotel** (`mercure hotel suites &
apartments`, 104 rooms → 93 photographed). The other seven — `al waleed
palace`, `copthorne lakeview`, `al bustan residence`, `pearl residence`, `the
address montgomerie`, `jumeirah creekside` (87 rooms!), `downtown hotel` —
returned **zero images each**.

**Why, and it is not a bug.** These are precisely the hotels whose names defeat
matching: Agoda rejected `The Weekend Address` for `the address montgomerie
dubai`, and `jumeirah creekside dubai` cleared no candidate at all.
`resolve_verified` then applies a *stricter* pair of gates (name ≥90 against the
title, ≤250 m). A name too awkward for Agoda's matcher is usually too awkward
for Booking's too. **The takeover is worth keeping — it rescued 93 rooms that
would otherwise have none — but it is a long shot, not a safety net.**

**The reporting defect this exposed.** Those seven hotels each incremented
`hotels_mapped`. "Mapped" says a plan was produced; it does **not** say
anything was found. So the headline number included hotels that delivered no
imagery whatsoever — the single thing this pipeline exists to deliver. Ten of
the first 92 mapped hotels (11%) were in that state.

Added `hotels_mapped_no_photo`, reported directly under the mapped total:

```
  mapped (nothing published yet) 92/106
    ├─ agoda identified it     84
    └─ agoda-blind (bookme+booking only) 8
    (of which found NO photo at all) 10   -- rooms planned, zero imagery; mostly agoda-blind
```

Placed under HOTELS, not in the ROOMS block, deliberately: room-level coverage
is already reported there, and an 85% room average **averages away** the
harsher fact that a whole hotel came away with nothing. Same class of defect as
D-74 — the number was right, the label implied a success it had not earned.

---

**D-76 — CRITICAL, CONFIRMED IN PROD: a scope switch relabeled the plan
checkpoint instead of clearing it, and a Nigeria-scoped run committed two
Dubai hotels to prod. Fixed.**

Reported by the operator from a real run's log (`20260821-035153`, Benin
City - Nigeria, city_ids `[163672]`, run on a separate machine). Phase 1
discovered exactly the 3 Nigeria hotels it should have — gate summary showed
`3/3`, no mismatch. Then Phase 2, which ran automatically because the wizard's
pause prompt was answered "n" (declining the pause, not declining Phase 2),
began iterating `[1/533]`, and item 1 was **Anantara Downtown Dubai**, item 2
**Nihal Residency Hotel Apartments** — real Dubai hotels, hundreds of hotels
away from anything Nigeria-related. Confirmed from the log's own print format
(`counts["rooms_inserted"] += n_rooms`, real COS uploads) that these were
**genuine prod writes**: 74 rooms / 433 images for Anantara, 1 room / 6 images
for Nihal Residency, both attributed to a run tagged "Benin City - Nigeria."
The operator cancelled with Ctrl-C on realizing this, before observing whether
the rest of the stale batch would have followed.

**Root cause, verified against the code, not inferred:**

- `PLAN_CACHE` (`plan.csv`) is ONE shared file across every city. Its scope is
  tracked only in a sidecar (`plan.csv.scope`); the CSV itself carries no
  per-row scope marker.
- `_load_plan(scope)` correctly detects a mismatch and returns `({}, set())`
  for THAT read — but does nothing to the file.
- `_append_plan_rows` is a pure append (by design, for per-hotel crash
  safety) and never rewrites or clears the file.
- Phase 1's entry point (`pipeline/run.py`, the `main()` discovery section)
  called `_load_plan(plan_scope)` then **unconditionally**
  `_save_plan_scope(plan_scope)` — relabelling the sidecar to the NEW scope
  regardless of whether the load matched. From that instant, every stale row
  still sitting in the CSV from the OLD scope silently reads as "valid for the
  new one."
- Phase 2's own read, later in the same run, trusts the file wholesale
  (filtered only by what the ledger already marked done, never by "is this
  hotel actually in THIS run's discovered set") — by design, so that an
  interrupted same-city run can resume cleanly. That design assumption is
  exactly what a cross-city relabel violates.

**The fix — `_load_plan(scope, adopt=False)`.** On a scope mismatch (wrong
schema OR wrong scope tag), if `adopt=True` the stale file is removed outright
via a new `_discard_plan_cache()`, not just left in place under a new label.
`adopt=True` is passed from exactly one call site: Phase 1's own entry point,
the only place that is about to relabel the file for a new scope right after.
Every other call site (`selftest`'s own diagnostic probe of a hypothetical
scope, Phase 2's read later in the SAME already-normalized run) keeps
`adopt=False` and is unaffected.

**What this does NOT change, and why it matters:** the exact case the
operator confirmed they still want — *"if 500 something hotels were unresolved
from Dubai and had to run... if I'm ever again running the pipeline on
Dubai"* — is untouched. A same-city, same-config resume hits the scope-MATCH
branch, never the mismatch one, so an interrupted Dubai plan is picked up
exactly as before. Only a genuinely DIFFERENT scope (a different city, or the
same city under changed matching config) now wipes the file instead of
quietly inheriting it.

**Verified two ways:**
1. Full selftest suite (11/11) — including the existing plan-checkpoint test,
   which depends on a diagnostic mismatch read (`adopt` defaulted off) leaving
   the file intact for a later same-scope read. Unaffected, confirming the fix
   doesn't regress the resumability the ledger's own test already covers.
2. A standalone reproduction of the exact reported scenario: check-point a
   Dubai hotel, switch to a Nigeria scope with `adopt=True`, check-point a
   Nigeria hotel, then read back as Phase 2 would. Before the fix this would
   return both hotel ids; after, it returns only the Nigeria one.

**Not yet done:** the machine that produced this log is not this one (a
PowerShell prompt in the log, `C:\Users\Bookme\Scraper-Agoda-Bookme-Dubai 4\`,
confirms a separate checkout). That machine's `out/cache/plan.csv` may
currently still hold a mixed-scope file from this exact incident. The fix
prevents the NEXT occurrence; it does not retroactively clean a file already
in this state elsewhere. Recommend checking/clearing `out/cache/plan.csv` and
`plan.csv.scope` on that machine directly.

**Also worth knowing, unrelated bug but same evidence:** the wizard's pause
prompt ("Pause and let you review the discovered plan before downloading
images and publishing starts? [Y/n]") answers **"n" as "don't pause"**, i.e.
proceed straight through to Phase 2 with no further confirmation. That is
documented behavior (mirrors `--yes`), not a bug, but it is worth knowing it
is why Phase 2 began immediately with no second gate to catch the leak before
it reached MySQL.

---

**D-77 — D-76's hotfix (adopt=True, wipe-on-mismatch) replaced with a
stronger design: one file PER SCOPE, no shared filename at all.**

The operator pushed back on the D-76 hotfix, correctly: wiping on mismatch
closes the cross-city leak, but it ALSO wipes a same-city checkpoint the
moment ANY other scope runs in between -- verified directly: a Dubai plan,
followed by a Nigeria run, followed by a return to the SAME Dubai scope,
came back `set()`. Confirmed by the operator's own question and by a live
test before any further change was made, not assumed.

**Why the hotfix still wasn't the right shape, even though it closed the
reported bug.** A mutable global path (`PLAN_CACHE`), reassigned once per run
and then read implicitly by every function, is the same risk SHAPE as the
sidecar tag it replaced -- correctness still depends on every caller, present
and future, remembering to set it in the right order before reading or
writing. The operator asked directly not to trade rigor for a smaller diff.

**The fix: `_load_plan(scope)` and `_append_plan_rows(..., scope)` each
resolve their OWN file from `scope` via `_plan_cache_path(scope) ->
plan-<md5>.csv`.** No shared global, no sidecar, nothing to keep in sync.
Consequences, each verified rather than assumed:

- **Cross-scope leak: impossible by construction**, not guarded against by a
  runtime check. Two different scopes are two different files; there is no
  code path left that could read one into the other.
- **Same-scope resume across an intervening, unrelated run: preserved.** A
  Dubai plan is untouched by a Nigeria run in between and is found again,
  unchanged, on return -- this is the actual point of per-hotel
  check-pointing, and D-76's hotfix had traded it away without saying so.
- **A second, worse variant closed for free:** two DIFFERENT scopes running
  CONCURRENTLY (not just sequentially) used to share one file under the old
  design -- a race, not just a sequential leftover. Per-scope files make that
  structurally impossible too; not requested, surfaced because the redesign
  implies it.
- **The header-schema guard is unchanged** (an old code version's file
  format under the same scope hash is still detected and removed) -- nothing
  from D-76's coverage was quietly dropped; it was checked line-by-line
  against what the old branch handled, not assumed equivalent.

**Verified, not asserted:**
1. Full selftest suite, 11/11. The plan-checkpoint test was rewritten to
   include the EXACT reported sequence -- Dubai discovers, a Nigeria scope
   starts fresh (asserted empty, not inherited), Nigeria's own commit-time
   read contains only its own hotel, and Dubai's checkpoint is read back
   afterward and asserted byte-identical to before the Nigeria run touched
   anything. This is a direct repro of D-76's incident, not a paraphrase.
2. Every prior assertion (round trip of images/size/category/provenance,
   resumability, hotel-set sensitivity, matching-config sensitivity, torn-row
   tolerance) preserved, none silently dropped.
3. Lint: baseline unchanged (34 errors before and after; one genuinely new
   finding in the added test, `nigeria_rows` unused, fixed immediately).
4. **Migration of this machine's real, uncommitted plan.** `out/cache/plan.csv`
   held today's live 168-hotel Dubai discovery (D-70/D-71's verification run),
   scope-tagged `30bab19aa4724acde2607599e8cab431` in its old sidecar. Renamed
   to `plan-30bab19aa4724acde2607599e8cab431.csv`; confirmed by recomputing
   `_plan_scope` from a live `--city Dubai --limit 200` query against prod and
   getting back the IDENTICAL hash, then loading the renamed file and getting
   back all 168 hotels. Nothing lost. (`plan.csv.scope`, now informationless
   once the hash is in the filename, removed.)

**Honest tradeoff, stated plainly, not left implicit:** each distinct
(hotel set, matching config) combination now keeps its own file
indefinitely -- no automatic cleanup exists. Bounded in practice (a handful
of scopes in real use, a few KB to a few MB each), but genuinely unbounded in
principle. Left as a `ponytail:` comment naming the ceiling (ADD a sweep
keyed on "every hotel in this plan reached hotels_done" if it ever actually
accumulates), not silently deferred.

---

**D-78 — Wiring audit + a real Phase-2 commit run caught a genuine (cosmetic)
summary bug: `[MISMATCH != 1]` on a run that actually behaved correctly.**

Full audit run: `--selftest` 11/11, repo-wide `ruff check .` (76 → 69 after
fixing 7 genuinely trivial findings: import sort in run.py/ledger.py, an
unused `noqa`, four unused unpacked variables -- all zero-behavior-change,
none touched anything structural), every CLI flag confirmed actually wired
to behavior (`a.<flag>` referenced downstream, none dead), every `pipeline/*`
module confirmed imported and used somewhere (no orphans). Two `B023`
(closure-over-loop-variable) findings inspected directly rather than trusted
to the linter's generic warning: both consume the closure synchronously,
within the same iteration, via `list(ex.map(...))` / `db.with_retry`'s direct
call -- confirmed false positives, not fixed, not left as an unresolved flag.

**The live flow (Phase 4/5): re-ran Benin City, Nigeria end-to-end, THIS time
letting Phase 2 commit for real** (`--city "Benin City" --city-id 163672
--yes --limit 3 --rooms-from both`, no `--plan-only`). Confirms D-77's fix
holds live, not just in the selftest: discovery scoped to exactly 3 hotels,
no Dubai bleed. One hotel (Eterno Hotels Limited) had rooms to publish;
Phase 2 committed it.

**Caught by the run itself, in real output, not by reading code:** the final
summary printed `Benin City - Nigeria · 1 hotels selected` and
`total 3/1 [MISMATCH != 1]` -- the pipeline's own accounting guard correctly
firing on a real inconsistency, exactly as D-49's design intends. Root cause:
`hotels_reached = i if work2 else hotels_planned` used `i` -- how far PHASE
2's OWN loop over `work2` walked -- as the run's reported scope. But `work2`
only ever contains hotels that had something to PUBLISH; a hotel Phase 1
discovered and found empty never enters it (the discovery loop `continue`s
before ever check-pointing one). So `len(work2) < hotels_planned` on almost
ANY ordinary run where some hotels come up empty -- not just an aborted one --
while `counts`'s other tallies (`no_listing`, `zero_rooms`) are populated
against the FULL Phase 1 population regardless. Two numbers from two
different populations, compared as if they were one.

**Fixed:** `hotels_reached = hotels_planned`, unconditionally -- matching the
`if not proceed:` branch already doing exactly this. `hotels_planned` is
right in every case: an ordinary full run, one stopped early in Phase 1
(handled the same way it always was), and one stopped early in Phase 2
(nothing after Phase 1 changes how many hotels were DISCOVERED, only how many
of those get published). Verified by replaying the actual run's own
manifest.json through the corrected function: `total 3/3 [ok]`.

**Not a data-integrity bug.** `manifest.json` (the machine-readable record)
already had the correct `hotels_attempted: 3` throughout -- this was purely a
terminal-display bug in the human-readable summary's denominator. Confirmed
by checking the actual data, not the printout:

| checked | result |
|---|---|
| `v2_common_hotels` row for `eterno-hotels-limited` | id 2495860, exists |
| `v2_rooms` where `v2_common_hotel_id=2495860` | **5 rows**, matches `rooms_inserted: 5` |
| `size_sqft` column | **now exists on prod** (215/323/344/377/431 populated) |
| `v2_attachments` (`attachable_type='...Room'`, `category='room-image'`) | **41 rows**, matches `attachments_inserted: 41` exactly |
| 3 sampled `cdn.bookmepk.com` URLs, live HTTP HEAD | **all 200, real `image/jpeg`, 88–120KB** -- genuine objects, not placeholder strings |
| `ledger_published.csv` | correctly recorded the publish |

**One wrong turn during verification, corrected before reporting anything:**
my first `v2_attachments` query joined on `attachable_id` alone and returned
49 rows that LOOKED like room images but were `attachable_type='...HotelReview'`,
`category='review-rating-image'` -- unrelated customer review photos that
happened to share numeric ids with these rooms in a polymorphic table. Caught
by checking the discriminator columns before reporting the number, not after.

**`images_uploaded` (46) vs `attachments_inserted` (41) -- a 5-room gap,
explained, not just noted.** `mirror_all_images` (which produces `uploaded`)
counts every image mirrored to COS, including each room's thumbnail;
`db.publish`'s `n_att` counts only NEW `v2_attachments` rows, and a room's
thumbnail is stored directly on `v2_rooms.thumbnail`, not duplicated into
`v2_attachments`. Exactly 5 rooms, exactly a 5-image gap. Consistent with
that explanation, not chased further as a live bug.

**Confirms, independently of D-77's own selftest:** `size_sqft`'s prod
migration (blocked earlier this session by a missing ALTER grant, D-62) has
since been completed -- by the operator, outside this pipeline's own
credentials, as the schema-adaptive code always expected.

---

**D-79 — The end-of-run cache sweep was a blanket delete, not a scoped one --
it defeated D-77's resumability guarantee one call site later, same day.**

Caught live, not by reading code: right after D-77 migrated and verified a
real 168-hotel Dubai plan under the new per-scope naming, an UNRELATED
3-hotel Benin City, Nigeria run (D-78's live verification) completed
successfully -- and the Dubai plan file was gone immediately after, with
nothing in that Nigeria run ever touching Dubai.

**Root cause:** the end-of-run sweep (`if proceed and not (_STOP or
agoda_dead): for _f in os.listdir(CACHE): ... os.remove(...)`) removed EVERY
file in `out/cache/` except ones prefixed `agoda_` -- unconditionally, on ANY
successful run, regardless of which scope's files they were. D-77 made plan
checkpoints resume correctly across an unrelated city running in between;
this sweep undid that guarantee the moment the unrelated city's OWN run
happened to finish cleanly, which is the ordinary case, not a rare one.

**Fixed:** the sweep now removes only `_plan_cache_path(plan_scope)` -- THIS
run's own plan file -- never anything belonging to a different scope.
Verified: selftest 11/11, lint clean, syntax valid.

**Correction made while writing this up, not after:** an earlier draft of
this entry also claimed `booking_waf.json` was affected by the same blanket
sweep. Checked before leaving it in: `booking.TOKEN_CACHE` resolves to
`out/booking_waf.json` -- the top level of `out/`, never inside `CACHE`
(`out/cache/`) at all -- so the sweep (`os.listdir(CACHE)`) never touched it,
before or after this fix. Same for the four orphaned `0N_*.json` files
sitting at the top of `out/`: unreferenced by any current code (checked by
grep), also outside `CACHE`, also never in scope for this sweep.

**Deliberately NOT restored: the sweep no longer touches `bookme_rooms.json`**
(previously caught by the same blanket `!= agoda_` inside CACHE, now simply
outside what this sweep removes at all). Judged safe to leave rather than
rebuilt scope-aware in the same pass: it already carries its own embedded
scope and self-invalidates on mismatch (D-56 predates this); its content is
discovered FACTS ("this room exists"), never a decision that can go stale, so
an unswept leftover is harmless to keep, only a few KB.

Both are the same LOWER-severity shape (unbounded accumulation, not silent
data leak) already accepted and documented for plan-*.csv in D-77's own
ponytail note. Not chased further here to stay proportionate to what this
pass actually needed to fix -- the operator is clearing the whole cache
directory outright immediately after this fix (starting the real prod
campaign fresh), which makes the accumulation question moot for now.
