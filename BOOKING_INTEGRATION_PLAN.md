# Booking.com as a second image source — integration plan

> ## Build status — 2026-08-17: **WIRED AND RUNNING**
>
> Every gate this document set has now been cleared, and the four blockers that
> kept it unwired are closed. The full attempt-by-attempt history is preserved
> in §8 — it is the most useful part of this file, because three of the four
> attempts failed *silently* and the reasons generalise.
>
> | piece | state |
> |---|---|
> | access over plain HTTP | ✅ working |
> | **WAF token** | ✅ **minted by the pipeline itself**, headless, ~4 s, self-healing on 202 — no `.env`, no operator step (D-33) |
> | room→photo extraction | ✅ 16/17 vs live-DOM ground truth, **0 mislabels** |
> | **identity resolution** | ✅ **name ≥90 AND ≤0.25 km, both required** (D-31) |
> | wiring into `map_rooms()` | ✅ gap-fill only; never replaces a match, never invents a room (D-34) |
> | date shapes | ✅ 1 base + free seed page, escalating only on zero (D-35) |
> | image mirroring | ✅ `cf.bstatic.com` serves to a plain session — no token, cookie or referer; verified end to end through `cos.mirror` |
> | cost | ✅ **~11 s per hotel that has gaps**, 0 s for hotels Agoda covered |
>
> **What finally made identity correct** was not a better name score. It was
> realising the two signals fail in *opposite* directions and requiring both:
>
> ```
> name alone      hilton dubai the walk  -> hilton-dubai-jumeirah-residence
>                 (token_set_ratio scores a SUBSET as a perfect 100)
> distance alone  pearl marina hotel apt -> lotus-grand-apartments-spa-marina
>                 (a DIFFERENT hotel, under 1 km, in a dense district)
> ```
>
> Measured across every candidate considered for 14 Dubai hotels, the two
> populations are cleanly separated: correct matches scored **name 100.0 at
> 0.028–0.103 km**; the best wrong candidate managed **72.7**, and the nearest
> wrong one sat at **0.360 km**. Thresholds sit inside those gaps.
>
> **A correction to this document's own history.** Attempt 2 and attempt 4 both
> recorded `hilton dubai the walk -> hilton-dubai-jumeirah-residence` as a wrong
> match. It is not — Booking slugs are historical, and that listing's *title* is
> today "Hilton Dubai The Walk". The scoring always ran against titles, so the
> pairing was right while the reasoning that produced it was still unsound. Both
> facts matter: the gate was correctly held, and the evidence used to justify
> holding it was partly misread (D-32). Never judge a resolution by its slug.
>
> **Still true, still accepted:** Booking sells individually-listed apartments
> inside the same building and geo cannot tell them from the hotel. Operator
> decision on record (D-30); the "never invent a room" rule (D-34) is what
> bounds the damage.

---

## 1. Why this source, in domain terms

The defect this project exists to fix is *a hotel-level photo shown against a
specific room*. Booking.com is the only source examined so far that **models
that distinction natively**:

```json
allRoomPhotos: [{ "id":"112496932",
                  "large_url":"…/max1024x768/112496932.webp",
                  "associated_rooms":["4884134"] }]
hotelPhotos:   [ … 45 separate hotel-level photos … ]
```

`associated_rooms` is an explicit per-room binding, and hotel-level photos live
in a **different array entirely**. Agoda gives us room photos too, but only as a
by-product of an availability grid; Booking states the relationship as a fact.

✅ **Measured — Park Hyatt Dubai, a hotel Agoda returns 0 rooms for:**

| | |
|---|---|
| named room types | 17 |
| room types with photos | **17 / 17** |
| room photos carrying an association | **78 / 78 (100%)** |
| hotel-level photos, kept separate | 45 |
| photos per room | 3–7 |

---

## 2. Use cases — the proposed one, plus four more

**A. Fill rooms Agoda could not match** *(your proposal — correct, and the
largest single win).* The last full-city run produced **1,226 unmatched Bookme
rooms**. Every one is a real supplier room sitting on a wrong photo. Booking is
a second chance at each.

**B. Rescue hotels with no Agoda identity at all.** 36 hotels ended
`no_agoda_match` — Booking is an independent identity space, so a hotel Agoda
cannot name may still resolve here. *(Distinct from A: this is hotel-level, not
room-level.)*

**C. Cross-source corroboration → a real confidence signal.** When Agoda and
Booking independently map the same Bookme room name to a room, that is two
sources agreeing — far stronger evidence than one 78% fuzzy score. This is the
single best way to **raise `ROOM_ACCEPT` confidence without losing recall**, and
to shrink the 230-row review queue. Disagreement becomes a review flag rather
than a silent coin-flip.

**D. A second `size_sqft` source.** ⚠️ Booking displays room size on most room
rows, but I have **not** verified the field is machine-readable in
`b_blocks_per_room_id`/`allRoomPhotos`. Verify before planning around it.
(Bookme was already proven to carry **no** size data at all, so Agoda is
currently a single point of failure for this column.)

**E. Room-name vocabulary enrichment for `match.py`.** Booking's naming is
noticeably cleaner than Bookme's rate-plan-laden strings ("King Room with
Lagoon View" vs "…Non Refundable (Package Rate)"). Harvested names are free
training data for the `TIERS`/`CLASSES` vocabulary whose gaps have already
caused one measured false-accept.

---

## 3. Hard constraints — these shape everything

### ✅ SOLVED: plain HTTP works. The browser is NOT required for fetching.

Earlier conclusion ("a real browser is mandatory") was **wrong** and is
superseded. The 202 was an **AWS WAF** challenge, not a header problem — the
giveaway was the `aws-waf-token` cookie in the browser session.

**The working recipe:**
1. Obtain `aws-waf-token` once from a real browser session (it appears in
   `document.cookie` after any successful page load).
2. Replay it as a cookie on `.booking.com` from an ordinary `requests.Session`.
3. **Send `Accept-Encoding: gzip, deflate` — NOT `br`.** This cost an hour:
   with brotli requested, `requests` cannot decode it, and you get 360 KB of
   binary that every marker check reports as "absent", which reads exactly like
   a bot-block. With brotli off: **3.7 MB of real HTML.**

| | no cookie | + `aws-waf-token` |
|---|---|---|
| status | `202` | **`200`** |
| bytes | 3,962 | **3.7 M** |
| `associated_rooms` | 0 | **82** |
| room photo URLs | 0 | **755** |

⚠️ **Unmeasured:** WAF token lifetime, and whether one token survives bulk
sequential use. Both are Phase-0 gates — if the token is short-lived, the
architecture needs a browser only to *mint* tokens, still not to fetch.

### ✅ MEASURED: throughput

n=6 real Dubai properties, plain HTTP, single connection:

| | |
|---|---|
| fetch | **3,830 ms** / hotel |
| parse | **15 ms** / hotel |
| payload | **4.2 MB** / hotel |
| **total** | **3.84 s / hotel** |

| workers | full 1,340-hotel city |
|---|---|
| 1 | 86 min |
| 4 | **21.5 min** |
| 8 | 10.7 min |

**Quality-per-minute verdict, which is what you asked for:** scoped to the
~40% of hotels that actually need Booking (unmatched rooms or no Agoda match),
4 workers ≈ **9 minutes** to attempt ~1,226 unmatched rooms. That is emphatically
*not* "an hour for 0.5%" — it is a candidate for every unmatched room in the
city for the price of a coffee break. **Proceed.**
⚠️ Concurrency >1 is untested against Booking's WAF; ramp 1→2→4 and stop at the
first challenge.

### ⚠️ Extraction: photos ✅, room NAMES need a real parser (do not use regex)

Room→photo binding extracts cleanly: **17 rooms / 78 photos** for Park Hyatt.
`allRoomPhotos` is a **JavaScript object literal** (unquoted keys, single
quotes) — `json.loads` fails; extract with a balanced-bracket scan.

**A trap I hit and must flag:** room names live in
`{"b_id":X, "b_blocks":[…thousands of chars…], "b_name":"NAME"}`. Pairing
`b_id`↔`b_name` positionally *looks* right and is **wrong** — it produced
`4884139 → "King Room with Skyline View"` when the browser's authoritative DOM
says `4884139 = Twin Room`. Only the first pair was correct; the rest drifted.
**Shipping that would put the wrong photo on the wrong room — the precise defect
this project exists to eliminate.**
**Required:** parse the room objects with a balanced-brace scanner (or read the
DOM in a browser), and **validate the id→name map against the browser's own
mapping as ground truth** before any write.

✅ **Availability-scoped, exactly like Agoda.** On a sold-out date
`allRoomPhotos` is empty while `hotelPhotos` survives. Booking does **not**
publish room identity for a property with no inventory, so it does not solve the
domain-impossibility from `DIAGNOSIS.md` §8b — it only widens coverage.

✅ **Identity resolution is the hard problem, and the obvious routes are shut.**
- `accommodations.booking.com/autocomplete.json` → `200` but **`{"results":[]}`**
  across six parameter shapes (`q`/`text`/`query`/`+types`/`+aid`).
- `booking.com/autocomplete_json.html`, `destinationfinder.json` → `404`.
- `booking.com/dml/graphql` → `405` on GET (exists, POST-only) ⚠️ unexplored.
- **Slug construction fails silently and dangerously**: a guessed
  `hilton-dubai-jumeirah-resort` returned Booking's **homepage** with HTTP 200.
  A naive scraper would parse that as "hotel with 0 rooms" — the same
  silent-wrong-answer class as the Agoda `suggest()[0]` trap (D-20).

> **Design rule that follows:** every resolved property must be **verified** —
> assert the landed page is a property page *and* that its identity matches the
> hotel we asked for (name + geo, the ladder `match_hotel()` already implements).
> Never trust a 200.

---

## 4. Proposed architecture

Booking is a **third source in an existing three-stage pipeline**, not a new
pipeline. It plugs in at the same seam Agoda occupies.

```
v2_common_hotels (identity: name, slug, lat/lon, country)
        │
        ├─► Bookme  /availability   → room NAMES        (the rows to fix)
        ├─► Agoda   room grid       → rooms + photos    (primary imagery)
        └─► Booking browser session → rooms + photos    (NEW: gap-fill + corroborate)
                    │
                    ▼
             map_rooms()  ── unchanged contract ──►  v2_rooms / v2_attachments
```

### 4.1 Where it hooks in

`map_rooms(bookme_rooms, ag_rooms, cat_ids)` already produces `unmatched`.
The integration is a **second matching pass over `unmatched` only**, using
Booking rooms, reusing `match.room_match()` verbatim — the veto system, the
disjointness rules and the rate-plan-variant fan-out (D-10) all apply unchanged.
**No change to the write contract, the additive-only guard, or the ledger.**

Provenance must be recorded per room (`source ∈ {agoda, booking}`) so a future
audit can tell which supplier's photo is on which row — currently unrecoverable
for Agoda rows, and a gap worth closing in the same change.

### 4.2 Robustness — multi-shape, like Bookme

Applying D-5/D-7 rather than trusting one config:

| axis | shapes | rationale |
|---|---|---|
| **date** | 4–6, spanning near *and* far (+4w … +40w) | ✅ availability-scoped; Park Hyatt needs ≥+30w |
| **occupancy** | `group_adults` 2 then 1 | mirrors `config.ALT_ADULTS`; different rooms surface |
| **identity route** | direct slug → site search → geo-verified pick | slug construction fails silently (§3) |
| **currency/locale** | pinned (`USD`, `en-us`) | a *condition*, not a default — pin it so results are comparable |

**A zero is never accepted from one shape** (D-7). Union across shapes; room
name + photo are **Identity** facts, so unioning is sound (D-19).

### 4.3 Time complexity — the honest cost

⚠️ **Throughput is unmeasured**; a browser page-load is the unit, roughly
5–15 s observed ad hoc. Do not commit to a number before measuring.

The mitigation is **scope, not speed**: Booking runs *only* on hotels that need
it — those with unmatched rooms or no Agoda match. In the last run that was
~40% of hotels, not 100%. Budget it as a **second pass**, resumable via the
existing checkpoint mechanism, never as a blocker on the primary path.

Concurrency: browser contexts are memory-heavy; start at **2–3 parallel
contexts**, measure, and treat Booking's tolerance as untested — ⚠️ it is a far
more aggressive anti-bot operator than Agoda, and getting blocked costs the
whole source.

---

## 5. Risks, and what each one costs

| risk | severity | mitigation |
|---|---|---|
| **Silent wrong-hotel resolution** (homepage-as-200) | **highest** — publishes another hotel's photos | assert property-page shape + name/geo verify; never trust HTTP 200 |
| Anti-bot escalation / IP block | high — loses the source | conservative pacing, low concurrency, back off on the first challenge, never retry into a block |
| DOM/selector drift | medium | prefer `window.booking.env.*` JSON over CSS selectors; fail loudly on shape change, never silently to zero |
| Browser resource exhaustion on long runs | medium | bounded contexts, restart per N hotels |
| Terms-of-use exposure | ⚠️ **your call, not mine** | this is scraping a competitor's site at scale; worth a deliberate decision before production |

---

## 6. Phased plan

**Phase 0 — measure what §3 leaves open (½ day, no production code).**
~~Throughput~~ ✅ **done: 3.84 s/hotel, 21.5 min per city at 4 workers.**
Still open: WAF-token lifetime and bulk-use survival; whether `size_sqft` is
machine-readable; the identity ladder's hit-rate on 30 real Dubai hotels;
whether the room→photo binding holds across ≥20 hotels (n=6 fetched ✅, n=1
fully verified against browser ground truth).
**Gate: if identity resolution is <80% reliable, stop — a wrong-hotel photo is
worse than a missing one.**
**Second gate: the id→name map must match the browser's DOM mapping exactly on
a 20-hotel sample before a single row is written** (see §3 — the naive join
silently mislabels rooms).

**Phase 1 — `pipeline/booking.py`, offline-testable.**
Identity ladder + room/photo extraction + the multi-shape prober. Selftest with
recorded fixtures so it runs without a browser, plus one live smoke test —
matching how `bookme.py`/`agoda.py` are structured.

**Phase 2 — wire into `map_rooms()` as a gap-fill pass** over `unmatched` only,
behind a `--rooms-from` extension. Add `source` provenance to published rows.
Measure: unmatched-room reduction, on the same city, against the current
baseline of 1,226.

**Phase 3 — corroboration mode (use case C).** Run Booking on rooms Agoda
*already* matched, compare, and use agreement to justify a threshold change.
Only after Phase 2 proves identity resolution is trustworthy.

---

## 7. What I am explicitly not claiming

- That Booking will fix the 24 domain-impossible hotels — ✅ **it will not**;
  Burj Al Arab returns 0 rooms there too, at a far date.
- That the 17/17 result generalises — n=1 property. Phase 0 must confirm.
- Any throughput figure. Unmeasured.
- That `size_sqft` is extractable. Unverified.

---

## 8. Identity resolution — the four failed attempts, preserved

Kept in full because three of the four failed **silently**, and the reasons
generalise well beyond Booking.com. Read this before proposing a fifth.

### Attempt 1 — derive the slug from the DB hotel name: **4/18 = 22%**
Wiring it would have attempted ~78% of hotels against a slug that does not
exist. The redirect guard did its job — all 14 misses raised, none silently
returned "0 rooms". Several DB names are also **truncated**
(`movenpick hotel jumeirah lakes tower`, `the apartments dubai world trade cen`),
which makes name-derived slugs worse than they would otherwise be.

### Attempt 2 — search + `score`: **21/30 = 70%, and the geo gate never fired**
`"latitude"` appears 26 times on the search page but **not inside the
property-card markup**, so every candidate arrived with no coordinates and the
distance check was skipped entirely — while the code read as though it ran.
Resolution therefore ran on name alone, and `match.score` (token_set_ratio)
returns 100 for a subset:

```
baity hotel apartments   -> bavaria-executive-suites          WRONG hotel, "100%"
cosmopolitan hotel       -> grand-cosmopolitan-dubai          questionable, "100%"
ambassador hotel         -> grand-ambassador                  questionable, "100%"
```

**The lesson is not "add geo".** It is that a guard which cannot fire looks
exactly like a guard that passes.

### Attempt 3 — `strict_score ≥90`: **15/30 = 50%, safer, still wrong**
It correctly rejected `cosmopolitan`, `ambassador` and `astoria` — and cost 20
points of recall to do it, while `baity → bavaria` still survived. **Name
matching alone cannot resolve this identity at any threshold**; tightening only
moves failures from *wrong* to *missing*.

### Attempt 4 — two-tier (free strict name, then geo): **15/20 = 75%, still unsafe**
The free tier had no backstop: `strict_score` is **not** subset-proof either —
introduced believing it was, it scored `hilton dubai the walk` ≥97. A free tier
with no verification is the name-only failure wearing a new name.

### What actually worked — attempt 5
Delete the free tier (D-29), then require **both** gates (D-31). The insight was
not a better score but that name and distance fail in *opposite* directions, so
either alone is unsound and both together are not. Rate held at ~75% while the
wrong matches went away — and the cost *fell*, because a high name bar leaves
most hotels with exactly one candidate to geo-check and hotels Booking does not
carry with none at all.

### One correction to attempts 2 and 4
Both recorded `hilton dubai the walk → hilton-dubai-jumeirah-residence` as a
wrong match. It is the **right** property — Booking slugs are historical and
that listing is titled "Hilton Dubai The Walk" today. Scoring always ran against
titles. The gate was still correctly held (the reasoning behind that acceptance
was genuinely unsound), but part of the evidence cited for holding it was
misread. Never judge a resolution by its slug (D-32).
