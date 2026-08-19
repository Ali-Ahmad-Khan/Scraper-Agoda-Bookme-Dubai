"""Tuning constants and the .env loader. One source of truth for both.

The pipeline's input is a CITY: `python -m pipeline.run --city Dubai` resolves
that name against Bookme's own `cities` table and reads the hotels to process
from `v2_common_hotels`. Hotel identity (name, slug, address, coordinates)
therefore costs zero network calls -- the public API is needed only for live
room NAMES, which exist nowhere in the database.

DESTINATION/COUNTRY_CODE below are FALLBACKS ONLY, used by the parts of this
project that are deliberately single-destination smoke tests (bookme.py and
agoda.py's own __main__ self-checks), never by a real run.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env():
    """Load .env from the project root. The path is explicit on purpose:
    a bare load_dotenv() walks up from the CALLER's frame and raises deep
    inside find_dotenv() when there isn't one."""
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))


DESTINATION = "Dubai"
COUNTRY_CODE = "ae"        # ISO-3166 alpha-2, used to build Agoda property URLs

GUEST_NATIONALITY = "PK"   # Bookme sells to Pakistan; drives which rates return

# Occupancy. 2, NOT 1 -- measured, against the intuition that a single adult
# dodges per-room occupancy limits and so returns more. It returns FEWER:
# doubles and twins are priced for two and drop out of some supplier feeds at
# adults=1. Same hotel, same date: 29 rooms at 1 adult vs 46 at 2 adults;
# another 15 vs 16. Leave at 2 unless a re-measurement says otherwise.
ADULTS = 2

# --- stay dates: weekend, one cycle out -------------------------------------
# Rooms on BOTH platforms are availability-scoped -- a room with nothing
# bookable on the probed night is simply absent, so the probe date decides how
# much of a hotel is even visible. Measured across the same hotels, a Saturday
# one full week past the coming weekend beat an arbitrary +45 days on 4 of 5
# hotels, sometimes decisively (6 -> 24 rooms; 0 -> 14, i.e. a hotel that was
# entirely invisible before).
#
# Why the weekend AFTER the coming one, rather than the coming one: it is far
# enough out that near-term bookings have not yet eaten the inventory, while
# still being a high-supply weekend night.
#
# This is a heuristic about a noisy system, not a law -- which is why the probe
# LADDERS below exist rather than this date being trusted absolutely. It is the
# starting point of the ladder, never the only shape asked.
STAY_WEEKS_OUT = 1         # 0 = the coming Saturday, 1 = the one after it
STAY_NIGHTS = 1

# --- probing: exhaustive by default, never threshold-gated -------------------
# RETIRED: ROOM_RETRY_BELOW. It gated whether a thin hotel earned more probing,
# on the assumption that a probe was expensive -- it was, when one Bookme probe
# meant a whole city-wide polling search (~379s). Keyed by slug that cost is now
# ~0.3s per hotel per shape, so the threshold bought nothing and cost coverage:
# a hotel sitting at 10 rooms was declared finished while more shapes were still
# finding more. Measured over 60 hotels x 4 shapes, unioning found +46% more
# rooms than the best single shape, and had NOT plateaued. Every hotel now runs
# the full ladder; a zero is only ever recorded after every shape has been asked.

# Every hotel gets all of these, unioned: (weeks_out, adults, nights).
# Widened along the axes that actually gate an availability-scoped grid --
# inventory is released in waves (weeks out), some rates only exist at 2+ nights,
# and doubles/twins drop out of supplier feeds at adults=1 while singles only
# appear there.
ROOM_PROBES = [
    (1, 2, 1), (1, 1, 1),
    (2, 2, 1), (2, 1, 1),
    (3, 2, 1), (1, 2, 2),
]

# Run ONLY for hotels still holding zero rooms after ROOM_PROBES. A zero is the
# one result this pipeline refuses to take at face value: if the property exists
# and is sellable at all, some date shape shows its rooms. These are the shapes
# that differ most from the base ladder rather than more of the same.
ROOM_PROBES_ESCALATION = [
    (4, 2, 1), (6, 2, 1), (8, 2, 1),
    (1, 3, 1), (2, 2, 3), (1, 2, 7),
    (4, 1, 2), (12, 2, 1),
]

# Midweek variants of every shape above, appended automatically. Business hotels
# release different inventory midweek than at the weekend, and a hotel invisible
# on a Saturday can be fully open on a Tuesday.
PROBE_MIDWEEK_OFFSET_DAYS = 3

# Concurrency for the per-hotel probe pass. Measured: 1 worker = 0.52 hotels/s,
# 8 = 3.76/s, 16 = 4.61/s with zero throttling or 429s at any level. Gains
# flatten past 8, so 8 is the bulkhead -- the same bound mirror_all_images uses.
ROOM_PROBE_WORKERS = 8

# A hotel Bookme answers with its permanent 500 ("property no longer available")
# is not re-probed across the remaining shapes. Verified permanent: 25
# consecutive calls across 5 hotels, plus 3 further dates each, never recovered.
# Set False to probe every shape regardless, at ~8x the calls for no measured
# gain.
TRUST_PERMANENT_UNAVAILABLE = True

# --- booking.com gap-fill ----------------------------------------------------
# Booking is a SECOND source, used only where Agoda left a Bookme room with no
# photographs. It is not a replacement: Agoda resolves ~100% of hotels by geo,
# Booking ~75%, so leading with Booking would lose coverage. Leading with Agoda
# and filling the holes only ever adds.
#
# Identity is geo-verified against the hotel's own coordinates on EVERY
# acceptance -- see booking.resolve_verified. Every wrong match ever measured in
# that module came from a name-only path, so there is no name-only path.
BOOKING_ENABLED = True

# Date shapes probed per property, ON TOP OF the undated property page that
# identity resolution already fetched and that costs nothing extra to parse.
# Booking's grid is availability-scoped exactly like Agoda's, so one date
# under-reports.
#
# SATURATION MEASURED 2026-08-17 -- an 8-shape sweep on 10 geo-verified Dubai
# hotels, tracking marginal new photographed rooms per shape added, in order:
#
#   (1,1) +72   (2,1) +18   (4,1) +3   (8,1) +2   (2,2) +0   (12,1) +1  (1,3) +0  (16,1) +0
#   cumulative:  72          90         93         95         95         96        96       96
#
# The first two shapes alone capture 94% (90/96) of everything an 8-shape
# sweep ever finds. BOTH run unconditionally as the base -- cheap (~2 page
# fetches) and reliably valuable enough that gating them behind a zero check
# would routinely under-probe a property that base shape 1 alone reported
# thin. `weekend_checkin` two weeks out costs nothing extra to justify.
BOOKING_PROBES = [(1, 1), (2, 1)]      # (weeks_out, nights)

# Escalation shapes, ordered STRONGEST MEASURED MARGINAL VALUE FIRST, run ONE
# AT A TIME by `booking_fill` -- not as a block -- stopping the moment either
# the caller's gaps are all matched or `BOOKING_ESCALATION_STOP` consecutive
# shapes add nothing. This is the adaptive quality/speed balance the fixed
# list (D-35) was replaced with: a genuinely thin property still gets chased
# through the full ladder, while a property that plateaus after shape 3 is not
# charged for shapes 4-6, which the same measurement shows rarely pay (D-42).
BOOKING_PROBES_ESCALATION = [(4, 1), (8, 1), (12, 1), (2, 2), (1, 3)]

# Consecutive escalation shapes adding ZERO new photographed rooms before
# giving up. 2, not 1: a single flat shape is not yet proof of a plateau on
# data this noisy (Booking, like Bookme, resamples rather than answers
# deterministically). KNOWN TRADEOFF: `hilton-dubai-jumeirah` measured
# 0,0,0,+1 across the four strongest escalation shapes -- with STOP=2 the
# loop gives up after the first two zeros and never reaches the shape that
# would have paid. That shape's AGGREGATE value was +1 room across 10 hotels,
# so the tradeoff is accepted for the calls it saves on the (more common)
# properties that really have plateaued; raise this if thin-hotel coverage
# turns out to matter more than the extra calls cost.
BOOKING_ESCALATION_STOP = 2

# IDENTITY: both of these must hold. Neither is sufficient alone, and that is
# the whole point -- they fail in opposite directions:
#
#   * NAME cannot separate two hotels in one complex. `hilton dubai the walk`
#     and `hilton dubai jumeirah` are 50m apart and both score 100 against each
#     other's listing (token_set_ratio rates a subset as perfect).
#   * DISTANCE cannot separate two unrelated hotels in a dense district.
#     `pearl marina hotel apartment` accepted `lotus-grand-apartments-spa-marina`
#     -- a different hotel -- at under 1km, live, before this was tightened.
#
# Requiring both closes both holes. MEASURED over 14 Dubai hotels, the two
# populations do not overlap anywhere near these values: every correct match
# scored name=100 at 0.028-0.103km, while the best wrong candidate managed
# name=72.7 and the nearest wrong one sat at 0.360km. The thresholds are set
# inside those gaps, not at their edges.
#
# Note the scores are computed against the candidate's TITLE, never its slug --
# booking.com slugs are historical (`hilton-dubai-jumeirah-residence` is today
# titled "Hilton Dubai The Walk"), so a slug that looks like the wrong hotel
# routinely is not.
BOOKING_MIN_NAME = 90
BOOKING_MAX_KM = 0.25

# NOT ADDED, deliberately -- a "thin hotel" / "agoda shortfall" trigger was
# considered and is unnecessary. Booking may never introduce a room type
# (D-34), so the only thing it can contribute is imagery for a room BOOKME
# already sells. Every such room that Agoda failed to match is already emitted
# as a row with no images, which is already a gap, which already sends the
# hotel to Booking. A separate trigger would only fire in the one case where
# there is nothing Booking is permitted to do.
# Cap on how many candidates may be geo-checked. Rarely reached now: at
# MIN_NAME=90 most searches leave exactly one candidate standing, and a hotel
# booking.com does not carry leaves NONE -- which is what makes an unresolvable
# hotel cost one search and no property fetches at all.
BOOKING_GEO_CANDIDATES = 3

# --- confirmed by Bookme, 2026-08 -------------------------------------------
# Allow a SOFT veto (tier / view -- a quality LABEL, not the physical room) to
# be overridden when something else positively corroborates the pair. CLASS,
# BEDROOMS and BEDS are never overridable: a suite is not a room, a 2-bedroom
# is not a 3-bedroom, a king is not a twin.
#
# Operator decision, 2026-08-18, on the CPO's framing: "it's better to have a
# photo of at least a room than some random landmark". The veto ladder was
# calibrated when the alternative to a match was NO photo; it is not -- the
# alternative is the hotel-level lobby shot this project exists to remove. And
# the direction is favourable: a Superior's photograph on a Deluxe room shows
# the guest no better than they will receive.
# Set False to restore hard tier/view vetoes exactly as before.
ROOM_SOFT_VETO_RESCUE = True

ROOM_ACCEPT = 75    # >= this score, a room's images are safe to auto-deliver
ROOM_REVIEW = 62    # 62-80 is delivered as a candidate for human review, never auto-applied

# How long a cached Agoda room list stays usable. The date-invariant half of a
# cached property (coordinates, isNHA, slug) never expires -- only the rooms
# do, because a hotel can renovate, rename or drop room types and nothing in
# the payload would say so. Rooms are NOT invalidated merely for having come
# from a different probe date: a room seen on any night is a real room, which
# is the same reasoning that makes the multi-probe union sound.
CACHE_FRESH_DAYS = 7

# --- publishing (DB + COS) --------------------------------------------------
# A hotel published less than this many days ago is skipped by the next run.
# Room imagery is identity, not availability -- it changes on the timescale of
# a refurbishment, so re-doing a hotel sooner spends 6 image round-trips per
# room to re-derive an answer that has not moved.
LEDGER_STALE_DAYS = 365

# Images kept per room: one thumbnail on v2_rooms + the rest as attachments.
# Agoda routinely publishes 15-30 near-identical shots of one room; past the
# first handful they are the same bed from a different corner, and every one
# costs a download AND an upload.
MAX_IMAGES_PER_ROOM = 6

# Below this many bytes an "image" is a placeholder, a spacer or an error page
# rendered as a JPEG -- cheap to detect, expensive to discover on the live site.
MIN_IMAGE_BYTES = 4096

# v2_rooms.name is varchar(191). Truncating is better than the insert failing
# mid-hotel, but a truncated name would silently break the dedupe key on the
# next run, so names are truncated ONCE here and the same value is used for
# both the insert and the dedupe lookup.
ROOM_NAME_MAX = 191

# A room scoring in the 62-75 review band: CSV only, no v2_rooms row. This was
# an explicit decision ("CSV only, no DB row") and stays the default.
#
# The tradeoff, for the record: a room scoring 70 vanishes from v2_rooms
# entirely while a room scoring 0 (case 2, no candidate at all) still gets a
# row -- so being *nearly* matched loses real supplier inventory that being
# *unmatched* keeps. Set True if that tradeoff should be revisited; it makes a
# review-band room get a row with no imagery (same shape as case 2) plus the
# CSV line, so nothing in v2_rooms is lost while a human decides.
REVIEW_BAND_CREATES_ROOM = False
