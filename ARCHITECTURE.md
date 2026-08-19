# Matching architecture

How this pipeline decides *which* Agoda room is *which* Bookme room, and why
each rule exists. For how to run the tool, see [`README.md`](README.md). This
document describes current behaviour, not a change log — see
`IMPLEMENTATION_PLAN.md` for that. The exception is a handful of ⚠️-marked
corrections below, kept because the wrong conclusion they replaced was
methodologically instructive (a cross-environment comparison, not a bad
measurement) and silently deleting it risks the same mistake recurring.

## The one framework everything else follows: identity vs. state

Every fact this pipeline touches is one of two kinds, and treating one as the
other is the source of most of the defects found and fixed in this project.

- **Identity** — what a thing *is*: a hotel's name, a room's name, its
  category, its images, its dimensions. Stable until the thing itself
  changes. Safe to **union** across multiple observations — a room seen on
  any one probe, at any occupancy, is a real room.
- **State** — what is *currently true*: availability, price. True at an
  instant and possibly never again. Merging state observations from
  different moments **fabricates a reality that never existed**.

This governs two load-bearing design choices:

- Room *names*, gathered across multiple search probes (different dates,
  different occupancy), are unioned. Doing the same with availability or
  price would be a correctness bug, not an optimisation.
- Room *categories* are computed once, deterministically, from the name —
  never inferred from price rank (a brand-name room's tier is never guessed
  from where it sits in the price list, because price ordering is state and
  category is identity).

## Hotel identity resolution

`v2_common_hotels` is authoritative for name, slug, address and coordinates —
no network call needed to know who exists. One matching problem follows from
that: joining a DB hotel to its Agoda listing (needed always). Joining to
Bookme no longer requires matching at all — see below.

### DB hotel → Bookme property: not a matching problem anymore

⚠️ **Historical correction.** Earlier versions of this document (and the
pipeline itself) described a deterministic slug-ladder here — exact slug,
then a trailing `-<digits>` stripped from either side, then a name+geo
rescue — because 18 sampled live-API slugs only matched the database exactly
5 times, and the live API's `CommonID` field appeared to join 0/40 times to
`v2_common_hotels.id`. **Both measurements were taken against `bookme_sky_uat`
compared against `api.bookmesky.com` (production) — a cross-environment
comparison.** Re-measured with the environments matched (UAT database against
`uat-api.bookmesky.com`):

| | cross-environment (superseded) | same-environment (current) |
|---|---|---|
| DB slug == live slug | 5/18 exact | **68/68 exact, 0 mismatches** |
| `Property.CommonID` → `v2_common_hotels.id` | 0/40 | **60/60** |

Within a matched environment, `v2_common_hotels.slug` **is** the live slug
Bookme's partner API expects, directly, with no transformation — see
`bookme.py`'s docstring. There is no ladder left to run: the slug ladder,
the `_SUFFIX` stripping, and the name+geo rescue for this specific join were
all deleted, not merely superseded. `CommonID` is now used as a positive
assertion (`common_id(payload) == hotel["id"]`), turning a silent wrong-hotel
write into a loud one — see `RELIABILITY.md` §2.7 for the checkpoint-scope
bug this class of cross-environment mismatch caused, and caught, later in
the same investigation. Full measurement trail:
[`AVAILABILITY_API_PROPOSAL.md`](AVAILABILITY_API_PROPOSAL.md).

### DB hotel → Agoda property

Name-first, geo-verified. Agoda's suggest endpoint proposes candidates from
the hotel name (escalating through name variants — parenthetical stripped,
destination appended, then the address as a last resort — only as needed,
since issuing all query forms for every hotel once tripled the request rate
and tripped Agoda's rate limiter a third of the way through a 465-hotel run).
Every candidate up to `top_n` is then geo-verified against the DB's own
coordinates, and the **closest confirmed** one wins — not the first
plausible one, because a same-building, individually-listed vacation rental
can carry the real hotel's name in its own title ("Luxury Burj
View...Kempinski Central Avenue...", 40m from the actual Kempinski) and score
100% on name while landing inside any plausible distance cutoff. Those are
filtered by Agoda's own `isNHA` flag, because neither name nor distance can
tell them apart alone.

`_confidence()` accepts on coordinates agreeing (`high`), or on same-city
**and** a strict name score with no geo check at all (`medium`) — the latter
path is what a same-city, different-hotel false accept would exploit.
**Measured, not assumed to be safe**: 123,170 pairs of genuinely different
hotels in the same city, across every country with a usable sample, and only
**0.145%** reach the `strict >= 88` threshold that path requires. No country
is an order-of-magnitude outlier (Australia, n=35,657: 0.29%). The residual
cases are systematic and covered by an independent gate: numbered
individually-listed units (`ecocrackenback 18` vs `ecocrackenback 12`,
`condo #25` vs `#24`), which Agoda's own `isNHA` flag already rejects.

**Country code comes from the database, per city — never guessed.** Two ways
this went wrong and were closed: a hardcoded fallback (`"ae"`, a Dubai-era
constant that would build `.../vienna-ae.html` for an Austrian hotel), and
trusting Agoda's own destination-name resolution, which matches on a NAME and
can resolve "Albanien" (German for Albania) to **Switzerland**, or "Rodriguez
De Mendoza" (Peru) to **Mexico** — both observed live, not hypothesised. A
wrong country code doesn't error — it builds a URL that never lands, the
browser fallback returns zero rooms, and the hotel is silently recorded as
having none. When the database itself has no country code (5 cities, 0
hotels in them, measured), the browser fallback is disabled for that run
rather than guessing — a smaller, honest loss instead of a confidently wrong
answer.

### Last-resort address matching: removal, not truncation

When name matching alone doesn't surface a candidate, the hotel's own address
is queried as a final fallback. Locality tokens have to be extracted from a
full postal string, and the *original* approach — truncate at the first
postcode-shaped digit run or the country name — turned out to be wrong by
construction, not just incomplete.

Measured over 4,000 real addresses in the catalogue: 93% carry a 4+ digit
postcode, 90% end with the country name — but field **order is not
universal**. `"vardanants 15/4, 0010, armenia, yerevan"` puts the city
**last**, after both the postcode and the country. Truncating at the first
postcode therefore deleted the city on exactly those addresses, and left **5%
of the catalogue (~4,400 hotels) with a string too short to query at all** —
the fallback silently dead for entire address styles (confirmed: Bangladesh
and Panama both returned nothing).

The fix is **removal**: strip the country token (and its initials, for
multi-word names like "United Arab Emirates" → "UAE") and any standalone 4+
digit run, keep everything else. Removal is order-independent, so no field
position can cost the city, and it degrades gracefully where truncation
failed catastrophically — a country name spelled differently than expected
(`"bosnia and herzegowina"` against a DB spelling of `"Herzegovina"`) simply
survives untouched in the string instead of taking the whole address down
with it.

One more real trap closed here: a country name is not safe to blanket-remove,
because plenty of cities embed their country's name — **Panama City**,
**Mexico City**, **Kuwait City** — and blanket removal turns `"Panama City,
Panama"` into a bare `"City"`, deleting the single most useful token. City
STATES are the same problem in the extreme: the country name *is* the city
name (measured: **8,613 hotels, ~10% of the catalogue**, sit in cities whose
name equals their country — this database stores several whole countries as
single cities, Brazil alone holding 6,732). `_strip_country_token()` only
removes an occurrence not immediately followed by "city", and removes nothing
at all when the city and country names are identical.

### DB hotel → Booking.com property: two gates that fail in opposite directions

The Agoda join above leans on distance *confirming* a strong name, with a
same-city + strict-name rescue for records whose coordinates are bad. Booking
needs a different shape, and the reason is worth stating precisely, because it
took four failed attempts to see it:

|  | what it cannot distinguish | live failure |
|---|---|---|
| name alone | two hotels in **one complex** | `hilton dubai the walk` vs `hilton dubai jumeirah` — 50 m apart, each scores 100 against the other's listing |
| distance alone | two unrelated hotels in a **dense district** | `pearl marina hotel apartment` → `lotus-grand-apartments-spa-marina`, under 1 km, a different hotel |

Neither is sufficient; together they are, because each covers exactly what the
other misses. Both are therefore **required** — name ≥ 90 *and* ≤ 0.25 km — and
there is deliberately no free path that skips either. Measured over every
candidate considered for 14 Dubai hotels, the populations do not overlap: correct
matches scored name 100.0 at 0.028–0.103 km, the best wrong candidate 72.7, the
nearest wrong one 0.360 km.

Two implementation details carry the correctness:

- **Coordinates come from the candidate's own property page**, never the search
  page — the search page's latitudes sit *outside* the property-card markup, so
  attributing one to a candidate needs a positional guess, the same join error
  that mislabelled rooms below. The page is not wasted: it is the same page the
  room fetch would need, and it is reused as a free date shape.
- **Scores run against the candidate's title, never its slug.** Booking slugs
  are historical — `hilton-dubai-jumeirah-residence` is titled "Hilton Dubai The
  Walk" today. A resolution report read by slug will look wrong when it is right.

The name gate also pays for itself: at ≥90 most searches leave one candidate
standing and a hotel Booking does not carry leaves none, so verification costs
about one page fetch per hotel instead of three.

## Three sources, one strict order — Agoda first, Booking covers what it could not

Agoda is the primary and stays that way: it is tried for every hotel, runs its
full escalation ladder, and its images always win. Booking.com is asked only
about what Agoda's pass left uncovered, in three distinct situations:

| Agoda outcome | what Booking does |
|---|---|
| matched, imaged every room | nothing — never even looked up |
| matched, imaged some rooms | fills the remainder (the ordinary gap-fill) |
| could not match the hotel at all, or was unreachable | supplies imagery for every Bookme room, so the hotel publishes instead of nothing |

The third row is a deliberate widening, not the original design. Booking
started as a pure gap-filler; making it also cover Agoda's total misses closed
a real hole — **18% of a measured 50-hotel baseline** had no Agoda match at all
and previously published nothing. In every row Booking still only ever supplies
**imagery for a room Bookme already sells** — it never introduces a room type
neither platform named, and never overwrites an Agoda match. A hotel Agoda
matched but rendered thin (say 3 of 15 Bookme rooms) is *not* a separate
trigger: the 12 unmatched rooms are already emitted imageless by `map_rooms()`,
which already makes them gaps, which already sends them to Booking — adding a
"thin hotel" or "Agoda-shortfall" heuristic on top was considered and rejected
as solving a problem the existing gap logic already solves.

**Agoda going unreachable degrades the run, it does not stall it.** A
production incident (2026-08-17) showed a fixed cooldown cannot outlast a block
longer than itself: 6 failures → 420s cooldown → 6 more failures → 420s
cooldown, forever, every hotel logging as `no_agoda_match` while the run looked
healthy. Three fixes, all in `pipeline/run.py` and `pipeline/agoda.py`: the
stand-down now **doubles** per cooldown with no success between (420 → 840 →
1680s, capped at 3600s, reset on any success); an outage is recorded as
`agoda_unreachable`, never conflated with a genuine `no_agoda_match`; and a
circuit breaker rotates the Agoda session at 3 consecutive unreachable hotels
and stops the run at 8, labelling the output folder `-AGODA-DOWN` so a
truncated run can never be mistaken for a complete one. Those hotels are not
lost work — they fall through to Booking exactly as the "unreachable" row above
describes, so the run keeps publishing while Agoda is refusing it.

## Room matching

### The tie-break and assignment must not depend on array order

`token_set_ratio` (the scoring metric) rates a **subset** as a perfect 100 —
"Junior Suite" scores 100 against both "Junior Suite" and "Junior Suite
Deluxe" — so exact ties are common, not exotic. Two array-position bugs of
the same underlying class were found and fixed:

1. **Within one Bookme room's candidates**, a plain `score > best` let
   whichever Agoda room happened to be listed first win a tie, even when a
   later one was the exact match. Fixed with a real tie-break: exact
   normalised-name equality first, then the smaller length gap.
2. **Across Bookme rooms**, each one grabbed its own best Agoda room in list
   order, so a contested Agoda room went to whichever Bookme room came first
   — even when a later Bookme room matched it far better. Fixed by scoring
   *every* viable pair once, sorting by strength, and assigning globally,
   greedy best-evidence-first. Array indices only break remaining ties, never
   decide them.

**Verified as a property, not a set of examples**: `map_rooms()`'s output
must be byte-identical regardless of the order either platform's room list
arrives in. `selftest()` shuffles both lists 60 times and demands identical
output every time — and this was mutation-tested against the pre-fix logic
to confirm it actually catches a regression: reintroducing list-order
assignment made 46 of 60 shuffles diverge.

### Hard vetoes: disjointness, not inequality

A room's **class** (what it physically is), **tier** (its quality level
*within* a hotel), **bedroom count**, **view**, and **bed configuration**
each veto a match when they disagree — a scoring penalty is the wrong
tool for a boolean domain fact, because "Executive Suite" vs. "Executive
Room" score ~90% on any string metric and are different products. As of
2026-08-18, **class, bedrooms and beds stay hard** (never overridable); **tier
and view are soft** (overridable — see "Corroboration", below).

The unifying rule, found through measurement rather than designed up front:
disagreement means the two sets are **disjoint**, never merely unequal. An
overlap means the names agree on at least one qualifier and differ only in
how much detail one side spells out — "Twin/Double Room" against "Double
Room" is very often one flexible room, and vetoing that would cost real
recall. Applied to four attributes that were each independently found broken:

- **Beds**: "King Deluxe Room with Balcony" vs. "Twin Deluxe Room With Sea
  View" scored 100% and would have put a king's photos on a twin — a real
  case found in production data. Fixed as a disjoint-set veto (`{king}` vs.
  `{twin}` — no overlap, vetoed; `{twin, double}` vs. `{double}` — overlap,
  not vetoed). Plural bed forms ("Two Queens") were invisible to the old
  singular-token match, meaning "no beds listed" and "beds listed as plural"
  were indistinguishable — now matches both forms.
- **Class** (`CLASSES`, e.g. room/suite/studio/apartment): `_first(n, CLASSES)`
  originally picked ONE class by list position, so a name stating two classes
  resolved by array order rather than evidence — "Family Suite Room" ->
  `suite` (precedes `room` in the list), then vetoed against "Family Room"
  even though neither side disagreed about anything. The same defect the
  tier fix below already closed, one attribute over, missed here until an
  operator's own examples surfaced it (2026-08-18). Now set-valued
  (`classes`); disagreement means no shared class word at all, exactly the
  tier treatment. Deliberately **not** silence-tolerant like tier/beds/view: a
  name stating NO class defaults to `{"room"}` rather than an empty set,
  because a bare tier word ("Deluxe") was measured matching "Deluxe Room",
  "Deluxe Suite", "Deluxe Studio" and "Deluxe Apartment" simultaneously — four
  different physical spaces with nothing in the name to choose between them.
  Accepting that is a coin flip, not recall; it stays vetoed on purpose, and
  is selftested explicitly so a future "helpful" fix doesn't quietly remove it.
- **Tier** (`TIERS`, e.g. deluxe/superior/executive): a name can legitimately
  carry two tier words ("Standard Deluxe Twin"), and picking one by
  first-match made the answer depend on the order of the tier list — the
  same array-position defect as the assignment bug above, one level down.
  Tier is set-valued; disagreement means no shared tier word at all.
  **`TIER_SYNONYMS` canonicalises vocabulary before that check runs**
  (`premium`/`premiere` -> `premier`, `luxury`/`luxe` -> `deluxe`,
  `classic`/`basic`/`value`/`essential`/`budget`/`economy` -> `standard`) —
  without it, "Premium Room" and "Premier Double Room" read as two disjoint
  tiers when the platforms simply used different words for the same rung.
- **View**: `view_of()` walks backward up to three tokens from the word
  "view", and without excluding tier words from that walk, "Deluxe Canal
  View" captured `"deluxe canal"` while "Deluxe City View" captured
  `"deluxe city"` — two genuinely different views that then *agreed* on
  "deluxe", while "Superior City View" and "Standard Superior City View"
  *disagreed* over a prefix that says nothing about the view. Both
  directions wrong, one cause: tier and accessibility words now belong to
  `VIEW_STOP`.

**Rate-plan, promotional and perk noise are stripped, not vetoed** — the
opposite treatment, because these describe the *offer*, a *promotion*, or a
*service entitlement*, never the room itself:
- `RATE_NOISE` strips offer terms (`non`, `refundable`, `smoking`, `package`,
  `rate`) wherever they appear, not just trailing, since platforms embed them
  mid-name ("Standard King Room (Package Rate)").
- `PROMO_TRIGGERS` handles marketing copy differently, because it is free text
  and unenumerable — but always a **suffix**, so the name is truncated at the
  first trigger word (`off`, `valid`, `including`, `save`, …) rather than
  stripped word-by-word. Measured live: `"Twin Room- 50% off on Grand Hyatt
  Water Park & 20% off on Food and Beverage (Valid Until August 2026)"` ->
  `"twin room"`. Note `"grand"` in that copy is *also* a `TIERS` word — left in
  place it manufactures a tier disagreement out of an advertisement.
- `PERK_NOISE` strips service entitlements (`lounge`, `access`, `butler`,
  `afternoon tea`, `concierge`) — a "Club" room is very often the identical
  physical room as its non-club twin, differing only in lounge access and
  breakfast, not in what gets photographed. Deliberately excludes
  view-carrying words (`beach`, `pool`, `sea`, `city`) — those name a real
  physical property, and an early version of this list included `beach` and
  broke `view_of()`'s own selftest by collapsing "Beach View" into "View".
- `SPLIT_WORDS` joins tokens the source splits that the vocabulary needs whole
  — `"De Luxe"` never matched `TIERS`' `"deluxe"` as two tokens, which cost
  four real review-band pairs their only corroborating signal.

**Accessibility is an absolute exception, immune even to corroboration.**
Every other attribute above treats one-sided silence as "not stated," never as
disagreement — but accessibility is the one place a mismatch is still
withheld from auto-publish, because a hotel marking a room accessible is
rarely silent about it, and showing a wheelchair user a standard bathroom (or
hiding a genuinely accessible room behind standard photos) is a worse
failure than a missing picture. It is capped just under `ROOM_ACCEPT` rather
than vetoed outright, so the pairing lands in `rooms_review.csv` **with the
candidate image and a decision column** — recoverable by a human — instead
of being silently discarded the way a veto would. `corroborations()` returns
`[]` outright on an accessibility mismatch, regardless of what else agrees,
specifically so the recall mechanism below cannot punch through this cap —
caught live by `_selftest_mapping`'s own accessibility assertion the moment
corroboration was wired up, which turned a deliberately-capped pair into an
auto-published one until this guard was added.

### Corroboration: raising recall without lowering the bar (2026-08-18)

Operator decision, CPO framing: *"it's better to have a photo of at least a
room than some random landmark."* The veto ladder above was calibrated when
the alternative to a match was NO photo; it is not — the alternative is the
hotel-level lobby/landmark photo this whole project exists to remove.

**Why a lower `ROOM_ACCEPT` threshold was rejected first.** The operator
supplied 11 labelled real pairs — 7 that should map, 4 that should not. Their
scores overlap completely (should-map 62.3–74.9, should-not 62.5–72.0), so no
threshold value separates them. What does, in all 11 cases: whether any
attribute **positively agrees** beyond the bare class word.

```
should map      Premier Room 2 Twin Beds  / Premier Double or Twin Room  -> tier, beds
                Apartment, 2 Bedrooms     / 2 Bedroom Apartment ...      -> bedrooms
                Family Room Summer Deal!  / Superior Family Room         -> word "family"
should NOT map   Captains Room             / Classic Room                 -> nothing
                Corner Suite              / Royal Suite                  -> nothing
                Twin Room                 / Standard Room                -> nothing
```

`match.corroborations()` checks four signals: shared tier (post-synonym),
matching bedroom count, overlapping beds, or a shared token that is not
generic filler (`GENERIC_TOKENS` excludes class/bed words, articles, and
digits — a hotel's own wing name, e.g. "Horizon", is exactly the kind of
distinctive shared word this is built to catch).

**What corroboration is allowed to do, and what it never can:**
- A **soft** veto (tier or view — `match.is_soft_veto()`) is overridden when
  the pair is otherwise corroborated. The direction is favourable by
  construction: the source is typically the lower tier, so a Superior's photo
  landing on a Deluxe room shows the guest no worse than they will receive.
- A pair scoring `ROOM_REVIEW <= score < ROOM_ACCEPT` with no veto at all is
  promoted to auto-publish when corroborated — this is the direct fix for the
  operator's 92-row review file.
- **Class, bedrooms and beds are never overridable**, corroborated or not — a
  suite is not a room, a 2-bedroom is not a 3-bedroom, a king is not a twin.
- **Accessibility is never overridable** (above).

Gated by `config.ROOM_SOFT_VETO_RESCUE` (default `True`); setting it `False`
restores the pre-2026-08-18 hard tier/view vetoes exactly, for a full rollback
without touching anything else in this section.

**Measured**, on the operator's own 92-row review CSV and 282-row unmatched-
with-candidate CSV from a real 100-hotel run:

| | before | after |
|---|---|---|
| operator's 7 should-map examples | 0/7 auto | 7/7 |
| operator's 4 should-not examples | — | 4/4 still held |
| review file (92 rows) | 0 auto | 78/92 (85%) auto, 0 vetoed |
| unmatched-with-candidate (282 rows) | 0 rescued | 68/282 (24%) rescued |

All 11 operator labels, plus the additional examples that surfaced the class
and synonym bugs, are asserted by name in `pipeline/match.py`'s `__main__` —
both directions, so a regression that re-breaks `Captains Room` -> `Classic
Room` fails the build, not just a regression that breaks a real match.

### Two data-quality bugs, fixed at the root, not the instance

- **Undecoded HTML numeric entities.** Confirmed live: Agoda's Spanish-market
  room names carry raw `&#xf3;` for 'ó' (`"Habitaci&#Xf3;N Superior Twin"`).
  Left alone, `norm()` never saw an accented character — it saw the literal
  text `&#xf3;`, torn into meaningless tokens (`xf3`, `and`, `n`) by the
  punctuation stripper, differing from every correctly-written name for the
  same word. `html.unescape()` runs first in `norm()`, before any other
  processing, so the entity decodes to the real character and folds
  normally.
- **Letter-spaced rendering artifacts.** One real hotel's room names arrived
  as `"T R I P L E"` / `"S U P E R I O R"` — and scored **77.8%** against
  each other, well inside the review band, because spaced-out text
  degenerates into single-character tokens that `token_set_ratio` treats as
  strong overlapping evidence regardless of which words they came from.
  Measured prevalence first (3 rooms, one hotel — not systemic) before
  choosing a fix general enough to cover the *class*: `norm()` collapses any
  run of 3+ single letters separated by spaces back into one word. Genuine
  single-letter room designators ("Room A", "Room B") need only one spaced
  letter and are untouched by the 3+ threshold.

## Recall vs. precision — calibrated deliberately for recall

A room this pipeline fails to match keeps the wrong hotel-level photo it
already had — a false negative buys nothing. A near-miss at least shows a
real room from the right hotel. Every threshold and veto above was measured
in **both** directions, not tuned against one metric:

| | before this pass | after |
|---|---|---|
| same-room name variants that still match (**recall**) | — | **100.0%** (0 lost, measured over 2,521 synthetic same-room pairs built from real published room names) |
| within-hotel room pairs correctly vetoed (**precision**) | 30.3% | **55.8%** |
| within-hotel false-accept risk (worst case, ignores tie-break) | 24.2% | **19.2%** |

The **worst-case** false-accept figure deliberately ignores that the
tie-break already picks the exact twin when one exists — a more realistic
"leave-one-out" simulation (hide a room, ask what the matcher would attach
in its absence) sits at ~74%, and decomposing *why* showed 84% of those are
either a strict subset of the true room's name or differ by ≤2 tokens (the
kind of near-miss a recall-first policy explicitly wants), with the
remaining ~15% genuinely far apart. This number is recorded, not chased
further — expanding the veto vocabulary to close it would trade real matches
for a benefit that doesn't survive the recall-first calibration above.

## Probe strategy (measured — do not re-derive without new evidence)

Room grids on both platforms are **availability-scoped**: a room with
nothing bookable on the probed night is simply absent, so the probe
date decides how much of a hotel is even visible.

- A weekend one cycle out beat an arbitrary +45 days on 4 of 5 test hotels
  (one went from 0 to 14 visible rooms — a hotel that had been entirely
  invisible).
- **2 adults, not 1.** The intuitive "one guest dodges occupancy limits" is
  backwards — doubles and twins drop out of supplier feeds at `adults=1`
  (46 vs. 29 rooms, same hotel, same night).
- No single probe wins for every hotel, so results are **unioned across
  probes**, deduped by room name — sound *only* because this catalogues room
  identity, never price or availability.

⚠️ **Superseded: the "thin hotel" threshold.** Both platforms used to gate
escalation on a room-count floor (`ROOM_RETRY_BELOW = 10`) — a hotel at or
above the floor was declared done, and only a hotel *below* it earned the
wider ladder. That threshold is gone entirely, on both platforms, by explicit
decision: **a zero is never the only signal that matters — a hotel sitting at
10, 50, or 100 rooms can still gain more from a wider probe, and there is no
principled count at which "probably done" becomes a safe assumption.**
Measured directly: unioning 4 Bookme probe shapes over 60 hotels found **+46%
more rooms** than the single best shape, and had **not plateaued** — a hotel
already well above the old floor of 10 was still gaining.

**Current rule, both platforms: run the full base ladder unconditionally for
every hotel, regardless of how many rooms it already holds; escalate further
ONLY a hotel that still holds genuinely zero after the full base ladder.**
A "zero" earned by giving up early is indistinguishable, downstream, from a
zero Bookme or Agoda actually reported — exactly the silent lie this whole
pipeline exists to prevent, now enforced against itself.

- **Agoda** (`_escalate()`): weeks beyond `STAY_WEEKS_OUT`, a 2- and 3-night
  stay, a midweek date, and single occupancy — every rung runs and unions,
  never stopping at the first non-empty result. Confirmed live that stopping
  early is unsafe: one hotel's 7 rungs converged on an identical result (a
  genuine inventory ceiling), indistinguishable, without running every rung,
  from a hotel where a later rung *would* have found more.
- **Bookme** (`harvest_rooms()`): 6 base shapes × weekend/midweek = 12 calls
  per hotel, unconditionally; a hotel still at zero afterward runs 8 further
  escalation shapes (further weeks out, 3 adults, longer stays) × 2 = 16 more.
  Nondeterminism makes this doubly necessary here, not just thoroughness: six
  identical calls to one slug, same date, same occupancy, returned 19, 18,
  27, 38, 36 and 44 rooms — a single call is not a measurement of a hotel on
  this endpoint at all.

**Booking runs a DIFFERENT rule: not "fixed ladder, escalate on zero" but
"adaptive, stop on a measured plateau."** It is asked only for hotels with
actual gaps (above), so unlike Bookme/Agoda it is never run unconditionally
for every hotel — and once it does run, the ladder itself does not either.

An 8-shape saturation sweep (2026-08-17, 10 geo-verified Dubai hotels, tracking
marginal new photographed rooms as each shape was added, in order) found:

```
(1,1) +72   (2,1) +18   (4,1) +3   (8,1) +2   (2,2) +0   (12,1) +1  (1,3) +0  (16,1) +0
cumulative:  72          90         93         95         95         96        96       96
```

The first two shapes alone capture **94%** of everything an 8-shape sweep ever
finds. Those two now run unconditionally as the base (`BOOKING_PROBES`).
Everything past them (`BOOKING_PROBES_ESCALATION`, reordered
strongest-marginal-value-first) runs **one shape at a time**, re-checking after
each whether the caller's actual gaps are now covered by the real matcher
(`map_rooms`, never a name-equality shortcut), and stops at 2 consecutive
shapes adding zero new photographed rooms (`BOOKING_ESCALATION_STOP`). A
genuinely thin property is still chased through the full ladder; a property
that plateaus after shape 3 is not charged for shapes 4–6, which the same
sweep shows rarely pay. Known, accepted tradeoff: a hotel needing exactly the
one shape after two flat ones is occasionally missed — that shape's aggregate
value was +1 room across 10 hotels, traded for the calls saved on the far more
common already-plateaued case.

**Extra Bookme payload axes were tried and rejected, on measured evidence, not
intuition.** `GuestNationality`, `Currency` and a multi-room `Rooms[]` array
looked like unexplored surface (Bookme's own docs imply nationality drives
rate visibility). Measured against the correct control — repeating the
*identical* call the same number of times — none beat plain repetition on
healthy hotels, and **0 of 11** hotels answering zero across the full ladder
were rescued by any combination of nationality, currency, or 32 further mixed
draws. Retested end-to-end through the real pipeline (not just raw Bookme room
counts) on 5 hotels: the imaged-room total moved **1.07×** — nowhere near
worth the extra calls. The zeros these axes were hoped to rescue are real UAT
inventory gaps.

## Concurrency: what's safe to parallelise, and a bug caught before shipping

Agoda's page API is deliberately, globally paced (`MIN_INTERVAL = 1.5s`,
process-wide state, confirmed necessary by a real block after bursting).
Threading around it would not help — the pacer's state is shared regardless
of thread count. Image mirroring is a genuinely different service pair
(source CDN, destination COS) with no such constraint, so it runs
concurrently (`ThreadPoolExecutor`, 8 workers) — see `README.md` for the
measured numbers.

**A real race was caught during that change, not shipped.** The first
version of the shared "already uploaded" cache (`cos.py`'s `_seen` set)
marked a key as done *before* confirming the upload had completed, so a
second thread racing on the identical image URL could receive a public URL
for an object that did not exist yet. Fixed by moving the mark to strictly
after a provable upload; a live regression test (`pipeline/cos.py`'s
self-check) races 8 threads against a single real URL on production object
storage and asserts every returned URL is immediately fetchable, not just
structurally identical.

## The additive-only guard, and its one named exception

Every statement in `pipeline/db.py` is routed through `_sql()`, which
inspects the verb and refuses anything that isn't `SELECT`/`INSERT`/`SHOW` —
at runtime, not by convention, because the DB user holds `ALL PRIVILEGES`
and the grant alone guarantees nothing. `_fill_empty_fields()` is the one
deliberate exception (see `README.md` for what it does); it does not go
through `_sql()`, and its safety is structural — `COALESCE` in the `SET`
clause, not the `WHERE` clause, is what actually makes the overwrite
guarantee true. `room_category_id` joined `thumbnail`/`size_sqft` under this
same exception 2026-08-17 (see below).

⚠️ **Correction: most `v2_rooms` rows are not written by this pipeline at
all.** An earlier full-city run left thousands of rows with `room_category_id
NULL` outside this pipeline's own inserts, and the working theory — an
uncoordinated second process, possibly a concurrently-edited copy of this code
— was wrong. Proven directly: on a hotel holding 0 rows, one single
`bookme.availability()` call — no pipeline, no `db.publish()` — created 5 rows,
with `hotel_id NULL` and `room_category_id NULL`, a signature `db.publish()`
cannot produce (it always writes `hotel_id=0` and a resolved category).
**Bookme's own backend writes `v2_rooms` as a side effect of being asked for
room names**, idempotently by name (repeated calls accumulate the union, never
duplicate). The probe ladder above asks that endpoint ~28 times per hotel, so
the uncategorised rows were the footprint of our own *reads*, not a second
writer. This also explains why the pipeline only ever categorised the rows it
inserted itself: `_fill_empty_fields()` now backfills `room_category_id` under
the same fill-only-if-NULL guarantee as the other two fields, closing the class
rather than the one symptom. One consequence worth stating plainly: **this
pipeline cannot avoid causing that write** — reading a room name from Bookme's
own API is itself the write, in software this pipeline does not own, including
during a `--dry-run`. The additive-only guarantee above is, and can only ever
be, scoped to this pipeline's *own* statements.
