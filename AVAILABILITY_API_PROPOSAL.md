# Proposal — replace the Bookme city-search room path with `/hotels/api/availability`

> **✅ IMPLEMENTED.** Section 6's architecture is now what `pipeline/bookme.py`,
> `pipeline/config.py` and `pipeline/run.py` actually run — every function
> listed in "what this retires" is gone from the codebase (confirmed: zero
> remaining references), and `--rooms-from`/the operator wizard no longer
> carry the old fixed-per-city cost warning this proposal made obsolete. Kept
> as the record of the measurements the decision was made from; the current
> behavior is documented in [`README.md`](README.md) and
> [`REPORT.md`](REPORT.md) instead.

**Status:** study complete, nothing implemented. Every number below was measured
live on 2026-08-12 against UAT + `bookme_sky_uat`, Dubai (`city_id` 1280).

> **Correction to the first draft of this document.** The first pass tested the
> **production** API (`api.bookmesky.com`) using slugs read from the **UAT**
> database. That is a cross-environment test and its conclusions were invalid —
> it produced a "17% of slugs resolve" figure and an elaborate slug-resolution
> proposal to fix a problem that does not exist. Corrected below: environment
> matched to environment, **the DB slug is the live slug, 68/68 exact.**
> The lesson is recorded because it is the same class of error the pipeline
> guards against elsewhere — comparing identifiers across two id spaces.

---

## 1. Verified contract

**Auth** — `POST https://uat-api.bookmesky.com/partner/api/auth/token`

```json
{ "username": "…", "password": "…" }   →   { "Token": "…", "ExpiryAt": "…" }
```

Token lifetime measured at **~24h**; one token served every call in this study
without re-minting. Sent as `Authorization: Bearer {Token}`.

**Rooms** — `POST https://uat-api.bookmesky.com/hotels/api/availability`

```json
{ "Currency": "PKR", "Slug": "hilton-dubai-the-walk",
  "CheckIn": "2026-08-13", "CheckOut": "2026-08-14",
  "Rooms": [{ "Adults": 2, "Children": [] }] }
```

`Currency` and `Slug` are required. Omitting `Rooms` silently defaults to
`Adults: 1` — the measurably *worse* occupancy (see `config.ADULTS`), so it must
always be sent explicitly.

**Response is shape-identical to `single-itinerary`**, so `bookme.rooms()`'s
existing parse and rate-plan collapse works unchanged. Field presence verified
across 54/54 room objects:

| field | present | note |
|---|---|---|
| `Name` | 54/54 | the join key |
| `MaxOccupancy` | 54/54 | |
| `AccurateMedia` | 54/54 | **`false` on 54/54** — the defect flag this project keys on |
| `Media` | 54/54 | |
| `Category` | 54/54 | |

---

## 2. The slug question — resolved, and it removes the whole problem

Against UAT, keyed by the slug straight out of `v2_common_hotels`:

| | result |
|---|---|
| DB slug → 200 | **68 / 68 returned the identical slug back** |
| slug mismatches | **0** |
| prod-form suffixed slug (`…-620`) against UAT | **500** — confirms separate namespaces |

`v2_common_hotels.slug` **is** the UAT live slug. No suffix, no ladder, no
resolution step. `_slug_keys()`, the `-<digits>` stripping, and the name+geo
fallback all exist to bridge a gap that only appears when you cross environments.

### And identity is now exact — this overturns Finding 9

| join | prod (old finding) | **UAT (measured now)** |
|---|---|---|
| `Property.CommonID` → `v2_common_hotels.id` | 0 / 40 | **60 / 60** |
| `Property.ID` → `v2_common_hotels.id` | — | 0 / 60 (different id space) |

`DB_FINDINGS.md` Finding 9 concluded *"the live API's CommonID does NOT join"* and
sent the design down a name+geo matching path. **That finding was measured against
production.** Within a matched environment, `CommonID` is an exact foreign key to
our own hotel id. Bookme-side identity becomes a lookup, not an inference — no
name scoring, no haversine, no false-match risk.

> This is worth more than the speed win. In the A/B below, the *old* path silently
> resolved `hilton-dubai-jumeirah` to `hilton-dubai-palm-jumeirah-741` — a
> different hotel, 0 room overlap, at a 100% name score. Exact-slug keying makes
> that class of error unrepresentable.

---

## 3. A/B — 5 Dubai hotels, same dates, same occupancy

OLD = prod hotel-scoped search → `single-itinerary`.
NEW = UAT `/availability` keyed by the DB slug.

| hotel | OLD rooms | NEW rooms | overlap | t_old | t_new |
|---|---|---|---|---|---|
| Hilton Dubai The Walk | 32 | 32 | 24 | 10.96s | 4.55s |
| Fairmont The Palm | 43 | **55** | 43 | 12.44s | 5.78s |
| Hilton Dubai Jumeirah | 17 *(wrong hotel)* | 13 | **0** | 32.16s | 3.58s |
| Anantara Downtown Dubai | 0 *(HTTPError)* | **9** | 0 | 11.10s | 8.46s |
| Shangri-La Hotel Dubai | 25 | **37** | 24 | 10.63s | 6.02s |
| **total** | **117** | **146** | 91 | **77.3s** | **28.4s** |

**+25% more rooms in 2.7× less time — and that excludes the ~379s city harvest
the old path needs in production but which this test skipped.** Room-name shape,
casing and rate-plan noise are the same on both sides, so `match.py` needs no
changes.

---

## 4. The operational catch: `/availability` is heavily nondeterministic

Six identical calls, same slug, same date, same occupancy:

```
hilton-dubai-the-walk    21  21  14  27  34  21     union 38   (best single 34)
fairmont-the-palm        55  42  64  53  55  53     union 64   (best single 64)
shangri-la-hotel-dubai   19  18  27  38  36  44     union 58   (best single 44)
hilton-dubai-jumeirah    13  21  39  13  13  21     union 42   (best single 39)
```

Same supplier fan-out nondeterminism as `/search`. **A single call is not a
measurement of a hotel.** This makes the existing union-across-probes strategy
*more* necessary, not less. Measured over 60 hotels × 4 probes:

| | best single pass | union of 4 | gain |
|---|---|---|---|
| hotels yielding ≥1 room | 24 | **28** | +17% |
| distinct rooms found | 495 | **723** | **+46%** |

**A 500 is permanent, not transient** — 25 consecutive calls across 5 known-500
hotels stayed 500 throughout. So `500` ≙ "not sellable", cleanly distinguishable
from a thin result, and it does not need retrying. (Coverage: **47%** of sampled
DB hotels yield rooms after union — consistent with Finding 10's "most catalogue
hotels are not currently sellable".)

**Transport faults are real and need retrying separately**: the host resets
connections under sustained load (`ConnectionResetError` observed twice). A
transport retry is not the same thing as a 500 retry and must not be conflated —
one is worth retrying, the other never is.

---

## 5. Performance

Throughput measured on real hotels:

| workers | rate | failures |
|---|---|---|
| 1 | 0.52 hotels/s | 0 |
| 8 | **3.76 hotels/s** | 0 |
| 16 | 4.61 hotels/s | 0 |

Zero throttling, zero 429s, no cooldown — unlike Agoda's 1.5s pace + 7-minute
circuit breaker. Gains flatten past 8 workers, so 8 is the sensible bulkhead
(and matches `IMAGE_WORKERS` already in `mirror_all_images()`).

**Full Dubai city, all 1,340 DB hotels, 4 probes:**

| | current | proposed | |
|---|---|---|---|
| wall clock | ~99 min | **~27 min** | **3.7×** |
| hotels covered | 469 *(only those in the prod search)* | **1,340** *(every DB hotel)* | **2.9×** |
| per-hotel cost | 12.7 s | **1.07 s** | **~12×** |
| targeted 5-hotel re-run | ~396 s *(pays a full city harvest)* | **~6 s** | **~66×** |

The last row is the operationally important one — it is the shape of every
`hotels_to_revisit.csv` re-run and every image backfill. Today a 5-hotel rerun
pays a 6.3-minute city harvest to fish 5 itinerary refs out of 620 properties.

---

## 6. Proposed architecture

Exactly as you framed it: **a "city run" becomes `SELECT … WHERE city_id = ?`
followed by one `/availability` call per slug.** No search, no polling, no ref
minting, no property indexing.

```
  v2_common_hotels WHERE city_id = ?        <- the hotel list, free, complete
        |
        |  for each hotel, for each probe (date x occupancy), 8 workers
        v
  POST /hotels/api/availability {Slug, CheckIn, CheckOut, Rooms}
        |
        |  union room names across probes   <- unchanged, and now load-bearing
        v
  CommonID == v2_common_hotels.id           <- identity assertion, exact
        |
        v
  existing Agoda match -> COS -> v2_rooms   <- untouched
```

Everything downstream of room-name collection is unchanged: matching, image
mirroring, COS, publishing, ledger, backfill.

**What this retires** (Bookme side only — all currently load-bearing, all become
dead once this lands):

| function | why it goes |
|---|---|
| `harvest_city()` | no city search |
| `index_properties()` | slug is exact; `CommonID` confirms |
| `_slug_keys()` / `_SUFFIX` | prod-only namespace artifact |
| `bookme.search()` / `_poll()` | polling gone |
| `bookme.resolve_place()` | no Place needed |
| `bookme.rooms()` | replaced (its *parse* logic is reused verbatim) |
| `search_ref_id` / `itinerary_ref_id` | **the entire concept disappears** |

The poll-ceiling truncation handling, the nondeterministic-search round unioning,
and the `Place`-resolution sibling-distance guard all retire with them.

**What must be built:**

1. **Token manager** — mint, cache, refresh on expiry (~24h) or on a 401. One
   function; credentials into `.env` alongside the existing secrets, never
   inlined.
2. **`availability()` in `bookme.py`** — reusing the existing collapse logic,
   with the two failure modes kept distinct: **retry transport faults, never
   retry a 500.**
3. **Bounded parallel probe loop** — `ThreadPoolExecutor(8)`, the pattern already
   proven in `mirror_all_images()`, unioning per hotel across probes.
4. **A `CommonID != hotel_id` assertion** — cheap, and it turns a silent
   wrong-hotel write into a loud failure.

Suggested sequencing: build (1)+(2) behind a flag with the existing path intact,
A/B a full city, then delete the retired functions in one commit once the
comparison is clean. Nothing above touches the DB write path, so the
additive-only guarantee is unaffected.

---

## 7. Open questions

1. **Is `/availability` a supported contract?** It is undocumented and absent from
   dev-tools traffic (SSR-only). If it can change without notice, that is an
   argument for keeping the old path behind a flag rather than deleting it
   immediately.
2. **Prod credentials + prod DB.** The architecture's one invariant is that
   **the DB and the API must be the same environment** — that is precisely what
   the first draft of this document got wrong. Moving to prod requires *both* the
   prod partner credentials and the prod DB, together. Worth stating explicitly in
   the runbook, because a half-migration silently reintroduces the 17% failure
   mode with no error to signal it.
3. **Does prod behave identically?** Coverage (47%), flakiness spread and
   `CommonID` join should be re-measured once against prod before cutover. The
   test scripts from this study are reusable as-is.
4. **How many probes?** The union gains +46% rooms over 4 probes and had not
   plateaued. Worth measuring 6–8 probes against the extra wall-clock, since each
   full pass now costs ~6 min rather than a city harvest.
