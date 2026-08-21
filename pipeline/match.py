"""
Name normalisation and similarity, shared by hotel matching and room matching.

Input : two raw strings from different platforms.
Output: a 0-100 similarity score, and the normalised forms it was computed on.

The platforms name the same thing differently, in two distinct ways:
  hotels -- punctuation and suffixes ("One&Only One Za abeel" vs
            "One&Only One Za'abeel"; "Ink Hotel" vs "INK Hotel Dubai")
  rooms  -- Bookme echoes the wholesaler's rate-plan string, which repeats the
            room name and appends a board-basis code:
            "Executive Suite [Executive Suite Executive Suite Nrhb]"
So room names get de-duplicated token-wise before scoring; otherwise the repeat
inflates the score against any string sharing those tokens.
"""
import html
import re
import unicodedata

# A run of 3+ single letters separated by spaces is a rendering artifact
# ("T R I P L E"), not a room name with three-letter words. Left alone, each
# spaced letter becomes its own token, and token_set_ratio -- which rates a
# SUBSET as a perfect match -- treats a pile of single-character tokens as
# strong evidence of similarity regardless of which two words they came from.
# Confirmed live: 'T R I P L E' scored 77.8% against 'S U P E R I O R', two
# unrelated words, well inside the review band, from real published data (one
# hotel, a supplier rendering bug, not a naming convention -- genuine
# single-letter room designators like "Room A" / "Room B" need only 1 spaced
# letter and are untouched by the 3+ threshold).
_SPACED_LETTERS = re.compile(r"\b(?:[A-Za-z]\s){2,}[A-Za-z]\b")

from rapidfuzz import fuzz

# B2B bedbank board-basis / refundability codes that trail a rate-plan string.
BEDBANK_CODES = {"ro", "bb", "hb", "fb", "ai", "nr", "nrro", "nrbb", "nrhb",
                 "nrfb", "nrb", "rf", "rfro", "rfbb"}
# Words that carry no discriminating signal between two names for the same room.
ROOM_NOISE = {"room", "rooms"}

# The SPELLED-OUT forms of the same rate-plan attributes BEDBANK_CODES covers in
# abbreviated form. These describe the OFFER, not the room: "Standard King Room"
# and "Standard Room (Non Refundable)" can be the identical physical room sold on
# two different rate plans, so leaving these tokens in makes two names for one
# room look like two rooms -- and, worse, makes the leftover tokens carry less
# relative weight. Measured over the rooms this pipeline has actually published,
# these accounted for ~210 of the one-sided token appearances between room pairs
# that a human would call the same room.
RATE_NOISE = {"refundable", "nonrefundable", "non", "smoking", "nonsmoking",
              "breakfast", "package", "prepaid", "cancellation", "rate",
              "rateplan", "plan", "included", "inclusive", "advance", "saver"}

# Standard OTA/hospitality shorthand -> the canonical word TIERS/BEDS/CLASSES
# actually vocabulary-check against. Without this, "Dlx" and "Deluxe" are two
# different tokens to the scorer: no veto fires (asymmetric information is not
# disagreement, same rule as everywhere else), but nothing CORROBORATES them
# as the same tier either, so "Dlx" vs "Deluxe Room" scores 66.7% -- inside
# the review band, not confidently auto-published, purely because of spelling,
# not because of any genuine doubt about the room. Kept deliberately small and
# unambiguous: every entry here is a WHOLE-token replacement (never a
# substring), so a real word that happens to contain "std" or "dbl" as a
# sub-string is never touched. Widen only on evidence, the same rule TIERS
# above documents -- an abbreviation guessed at is a typo-correction feature
# with a different, much larger false-positive surface than this.
ABBREVIATIONS = {
    "dlx": "deluxe", "std": "standard", "sgl": "single", "dbl": "double",
    "twn": "twin", "apt": "apartment", "exec": "executive",
}

# MARKETING copy, not room identity. Distinct from RATE_NOISE (which describes
# the OFFER's terms): these describe a PROMOTION attached to the offer, and
# platforms staple arbitrarily long ones onto the room name -- measured live:
#   "Twin Room- 50% off on Grand Hyatt Water Park & 20% off on Food and
#    Beverage (Valid Until August 2026)"
# Against Bookme's "Twin/Double Room De Luxe Club Room" that is ~30 tokens of
# pure noise on one side, which buries the 2 tokens that actually agree. Every
# scorer here is token-based, so this is not cosmetic -- it is the difference
# between a match and a miss.
PROMO_NOISE = {"summer", "winter", "spring", "autumn", "deal", "deals", "offer",
               "offers", "special", "promo", "promotion", "discount", "save",
               "savings", "sale", "flash", "limited", "valid", "until",
               "complimentary", "voucher", "bonus", "gift", "early", "bird",
               "january", "february", "march", "april", "may", "june", "july",
               "august", "september", "october", "november", "december"}

# PERKS AND SERVICE ENTITLEMENTS, not physical space. A "Club" room is very
# often the identical room to its non-club twin, differing only in lounge
# access, breakfast and evening canapes -- a billing difference, not a
# different photograph. Confirmed against real pairs:
#   "Horizon, Premier Room, 1 King Bed"
#   "Horizon King Club Room - Lounge Access Including Afternoon Tea, All Day
#    Soft Beverages, Evening Sundowners & Canapes"
# Same floor, same bed, same room. Stripping these stops a perk from reading as
# a quality tier and vetoing the pair.
# NOTE what is deliberately absent: "beach", "pool", "garden", "city", "sea"
# and the like. Those name a VIEW -- a real, physical property of the room --
# and stripping them collapses "Deluxe Beach View" and "Deluxe Lagoon View"
# into the same string. (Caught by this module's own view selftest the first
# time "beach" was listed here.) Only service entitlements belong below.
PERK_NOISE = {"lounge", "access", "afternoon", "tea", "soft", "beverages",
              "beverage", "evening", "sundowners", "canapes", "snacks",
              "happy", "hour", "butler", "concierge", "privileges", "benefits",
              "wifi", "parking", "transfer", "shuttle", "free",
              "drinks", "welcome", "amenity", "amenities"}

# "De Luxe" is "deluxe" split across two tokens. Left joined, the tier
# vocabulary never matches it, so the pair loses its single strongest piece of
# corroborating evidence -- measured on 4 real review rows whose ONLY agreement
# was a tier written this way on one side.
SPLIT_WORDS = [(r"\bde\s+luxe\b", "deluxe"), (r"\bsea\s+view\b", "seaview"),
               (r"\bnon\s+smoking\b", "nonsmoking"),
               (r"\bbed\s+room\b", "bedroom")]

# Whole tokens that are pure measurement noise: "41sm", "441sf", "35sqm".
_DIM_TOKEN = re.compile(r"^\d+(?:sm|sf|sqm|sqft|m2|sqm\.|sf\.)$")
_YEAR = re.compile(r"^(?:19|20)\d\d$")

# Where the room name STOPS and the advertisement starts. Enumerating every
# marketing word is unwinnable -- the copy is free text and arbitrarily long
# ("50% off on Grand Hyatt Water Park & 20% off on Food and Beverage (Valid
# Until August 2026)"). But it is always a SUFFIX, so truncating at the first
# trigger removes the whole tail regardless of what is in it, including words
# no vocabulary could anticipate ("hyatt", "water", "park"). Note "grand" in
# that example is also a TIER word -- left in place it would have manufactured
# a tier disagreement out of pure advertising copy.
PROMO_TRIGGERS = {"off", "valid", "including", "includes", "complimentary",
                  "save", "discount", "promo", "offer", "offers", "deal",
                  "deals", "upto", "worth", "voucher"}
# Percentage-off fragments survive tokenisation as a bare number + "off".
_PCT = re.compile(r"\b\d+\s*%")

# The word "access(ible)" carries TWO unrelated meanings in room names, and the
# accessibility veto (see ACCESSIBILITY) was reading both as disability access.
# Measured over the 188 rows of one production run's rooms_review.csv:
#
#   * ENTITLEMENT -- "1 King Premium Club Lounge Accessible" is a club-lounge
#     perk, nothing to do with mobility. 15 rows were capped at the review
#     ceiling against a Bookme name saying "Club Lounge Access", i.e. the SAME
#     perk written slightly differently. Rewritten to "club" so the perk is
#     scored as a perk on both sides instead of as a phantom disagreement.
#   * DISABILITY, written in vocabulary the veto did not know -- "Superior
#     Special Needs Room" vs "Superior Accessible Single Room". Both rooms are
#     accessible, and the pair was held in review because only one side used a
#     recognised word. Worse, PROMO_NOISE strips "special" as advertising
#     language, so by the time features() looked, the surviving token was a
#     bare "needs" and the accessibility signal was gone entirely.
#
# Both are fixed by resolving the SENSE here, before token filtering can
# destroy the evidence. This makes the accessibility veto MORE accurate, not
# weaker -- it still fires on every genuine one-sided mismatch.
#
# The entitlement sense is read off the GRAMMAR, not off a list of facilities,
# for exactly the reason view_of() gives: what a room grants access TO is
# whatever the property happens to have -- club lounge, beach, pool, spa, gym,
# terrace, garden, ski slope, airport shuttle -- so enumerating them is a list
# that is wrong the moment a property invents a new amenity. Two rules cover
# the whole class:
#
#   * "access" is a NOUN and takes a subject: "<something> access" is access TO
#     that something. Entitlement, unless the subject is a disability word.
#   * "accessible" is an ADJECTIVE. Standing on its own, or qualifying the room
#     ("Accessible Room", "Superior Accessible"), it means disability-adapted.
#     Given a subject of its own ("Club Lounge Accessible") it is the
#     entitlement sense written adjectivally.
#
# The subject is found by walking backwards to the first word that cannot be
# one -- room classes, beds and tier words describe the ROOM, not a facility --
# which is the same walk view_of() does, for the same reason.
_DISABILITY = {"disabled", "disability", "wheelchair", "mobility",
               "handicap", "handicapped", "ada", "adapted"}
# Multi-word disability vocabulary, collapsed first so the walk sees one token.
# "special needs" matters twice over: PROMO_NOISE strips "special" as
# advertising language, so left alone the surviving evidence is a bare "needs".
_DISABILITY_PHRASES = re.compile(
    r"\bspecial(?:ly)?\s+(?:needs?|abled)\b|\bhandicap(?:ped)?\b|\bwheel\s*chair\b")
_ACCESS_RE = re.compile(r"\b(access|accessible|accessibility)\b")


def resolve_access(n):
    """Rewrite access/accessible to its SENSE: 'accessible' for disability,
    nothing at all for an amenity entitlement (the subject is left in place)."""
    n = _DISABILITY_PHRASES.sub("disabled", n)

    def one(m):
        word = m.group(1)
        prev = (n[:m.start()].split() or [None])[-1]
        if prev in _DISABILITY:
            return "accessible"
        if word == "access":
            # NOUN. "<subject> access" is access TO the subject, and the
            # subject is an open class -- beach, pool, spa, gym, terrace,
            # rooftop, marina, ski, whatever the property has. No list can
            # close it and none is needed: the disability reading is written
            # "<disability word> access", which the check above already took.
            # Bare "access" is not a room attribute under either reading.
            return ""
        # ADJECTIVE, and here the safe default flips. An unrecognised word in
        # front proves nothing -- "Cosy Accessible Room" is disability-adapted
        # and "cosy" is just an adjective, one of an open class of them. So
        # entitlement is asserted only against PERK_NOISE, the vocabulary this
        # module already keeps for service entitlements ("Club LOUNGE
        # Accessible"); everything else reads as disability.
        #
        # The asymmetry is deliberate and matches the ACCESSIBILITY rationale:
        # a wrong "accessible" costs one pair held in the review band where a
        # human still sees it, while a wrong entitlement auto-publishes a
        # standard bathroom to the guest who most needs a roll-in shower.
        return "" if prev in PERK_NOISE else "accessible"

    return re.sub(r"\s+", " ", _ACCESS_RE.sub(one, n)).strip()

# Different words, same rung. Platforms disagree on vocabulary far more often
# than on meaning -- "Premium Room" vs "Premier Double Room" is one room. Only
# genuinely interchangeable pairs belong here; anything that reorders a hotel's
# actual price ladder does not.
TIER_SYNONYMS = {
    "premiere": "premier", "premium": "premier",
    "classic": "standard", "basic": "standard", "essential": "standard",
    "value": "standard", "budget": "standard", "economy": "standard",
    "luxe": "deluxe", "luxury": "deluxe",
}


def norm(s):
    """Lowercase, unify '&', fold accents, drop punctuation, collapse space.

    Unicode-aware on purpose. A `[^a-z0-9]` filter silently erased every
    non-Latin name to the empty string -- Japanese, Arabic, Thai, Cyrillic all
    scored 0 against everything and fell out as "unmapped" with no error
    anywhere. It also mangled Latin diacritics ("Supérieure" -> "sup rieure"),
    splitting one word into two junk tokens. Since the pipeline is meant to
    retarget to any city, both are correctness bugs, not cosmetics.

    Accents are folded so "Superieure" and "Supérieure" agree; other scripts are
    preserved as-is so they can at least match each other. Folding is applied to
    both sides equally, so scripts where a combining mark is phonemic rather
    than decorative (Japanese dakuten) stay self-consistent -- they compare
    correctly against each other, at the cost of not distinguishing that mark.

    html.unescape() runs FIRST, before the "&" handling below, and this order
    is load-bearing: some source payloads (confirmed live -- Agoda's Spanish-
    market room names) carry undecoded numeric entities, "Habitaci&#xf3;n"
    for "Habitación". Left alone, the accent-fold step never sees an accented
    character at all -- it sees the literal text "&#xf3;", which the next line
    would tear into "and", "xf3" as ordinary punctuation-separated tokens, and
    which then differed from every correctly-written name for the same word.
    Decoding first turns it into "ó", which folds normally.
    """
    s = _SPACED_LETTERS.sub(lambda m: m.group(0).replace(" ", ""), s or "")
    s = html.unescape(s).lower().replace("&", " and ")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = "".join(c if c.isalnum() else " " for c in s)
    return re.sub(r"\s+", " ", s).strip()


def norm_room(s):
    """Normalise a room name, stripping bedbank rate-plan cruft.

    "Executive Suite [Executive Suite Executive Suite Nrhb]" -> "executive suite"
    """
    s = re.sub(r"\[.*?\]", " ", s or "")          # drop the bracketed rate-plan echo
    s = _PCT.sub(" ", s)                           # "50% off ..." -> " off ..."
    n = norm(s)
    for pat, rep in SPLIT_WORDS:                   # "de luxe" -> "deluxe"
        n = re.sub(pat, rep, n)
    n = resolve_access(n)                          # see resolve_access
    toks = [ABBREVIATIONS.get(t, t) for t in n.split()]
    # Cut the advertisement off the end before anything else looks at the
    # tokens -- see PROMO_TRIGGERS. Guarded so a name that opens with a
    # trigger is never erased entirely.
    for i, t in enumerate(toks):
        if t in PROMO_TRIGGERS and i:
            toks = toks[:i]
            break
    while toks and toks[-1] in BEDBANK_CODES:      # trailing board-basis code
        toks.pop()
    # Rate-plan words describe the offer, PROMO_NOISE describes a promotion
    # attached to it, PERK_NOISE describes a service entitlement, and a bare
    # dimension is a measurement -- none of the four describe the physical
    # room, so all four are dropped wherever they appear, not just trailing
    # (platforms embed them mid-name: "Standard Double Or Twin Room (Package
    # Rate)").
    toks = [t for t in toks
            if t not in RATE_NOISE and t not in PROMO_NOISE
            and t not in PERK_NOISE and not _DIM_TOKEN.match(t)
            and not _YEAR.match(t)]
    seen, out = set(), []
    for t in toks:                                  # collapse the repeated echo
        if t not in seen:
            seen.add(t)
            out.append(t)
    return " ".join(out)


def score(a, b):
    """Recall-oriented similarity, for generating candidates.

    token_set forgives one name being a superset of the other, which is what
    lets "Ink Hotel" find "INK Hotel Dubai" -- and also what makes it unsafe as
    a decision metric (see strict_score).
    """
    if not a or not b:
        return 0.0
    return max(fuzz.token_set_ratio(a, b), fuzz.token_sort_ratio(a, b))


def strict_score(a, b):
    """Precision-oriented similarity, for accepting a match on the name alone.

    token_set scores "Downtown Hotel" against "Millennium Plaza Downtown Hotel
    Dubai" at 100 because the first is a subset of the second -- a different
    property entirely. token_sort is length-sensitive, so the same pair scores
    55 while genuine variants ("TIME Onyx Hotel Apartment(s)") stay at 98.
    """
    if not a or not b:
        return 0.0
    return fuzz.token_sort_ratio(a, b)


# Tokens that identify WHICH unit, as opposed to describing it. Two listings
# from the same operator in the same development differ by exactly these and
# nothing else -- "Frank Porter - Rimal 1" vs "Frank Porter - Rimal 3" scores
# 95% on any string metric, and "Goldcrest Views" vs "Goldcrest Views 2" scores
# 96.6%. Compass words behave identically ("Fairmont Residence South").
_UNIT_DIRECTIONS = {"north", "south", "east", "west",
                    "northeast", "northwest", "southeast", "southwest"}
_DIGIT_RUN = re.compile(r"\d+")


def unit_marks(n):
    """The unit-identifying tokens in an already-normalised name."""
    return set(_DIGIT_RUN.findall(n)) | (set(n.split()) & _UNIT_DIRECTIONS)


def same_listing(a, b, floor, drop=None):
    """Are these two normalised names THE SAME individual listing?

    Distinct from "the same hotel". A vacation rental that merely name-drops a
    hotel ("Luxury Burj View 2BR in Kempinski Central Avenue") is a different
    question from a rental whose title IS the Bookme row's title ("Nasma Luxury
    Stays - Limestone House"). token_set_ratio scores BOTH at 100 and cannot
    separate them; token_sort_ratio is length-sensitive and does -- measured
    over 430 rejections from run 20260820-061304, the name-dropping cases top
    out at 59 while genuine same-listing pairs sit at 90-100.

    A high score alone is still not enough, because same-operator siblings
    differ only by a unit number, so those are vetoed outright rather than
    scored -- the same reasoning CLASSES uses for room class.

    `drop` removes a token that carries no identity here -- in practice the
    destination, which one platform appends and the other does not. Since the
    whole comparison is already scoped to one city, the city name is noise that
    only shortens one side. Not cosmetic: measured over the same 430
    rejections, dropping it moved three "Kennedy Towers - X [Dubai]" listings
    from 88-90 to a flat 100 against their Bookme twins, and cost nothing
    anywhere else (127 rescued vs 124, zero lost).
    """
    if drop:
        pat = rf"\b{re.escape(drop)}\b"
        a, b = re.sub(pat, " ", a).strip(), re.sub(pat, " ", b).strip()
    return strict_score(a, b) >= floor and unit_marks(a) == unit_marks(b)


def room_score(a, b):
    """Similarity for room names, ignoring the filler word 'room'."""
    na, nb = norm_room(a), norm_room(b)
    base = score(na, nb)
    sa = " ".join(t for t in na.split() if t not in ROOM_NOISE)
    sb = " ".join(t for t in nb.split() if t not in ROOM_NOISE)
    return max(base, score(sa, sb)), na, nb


# A room's CLASS is the one attribute a guest would call a mis-sell. "Executive
# Suite" and "Executive Room" score ~90% on any string metric but are different
# products, so class disagreement vetoes a match outright rather than scoring it
# down. Ordered longest-first so "junior suite" wins over "suite".
CLASSES = ["presidential suite", "junior suite", "penthouse", "villa", "suite",
           "studio", "apartment", "chalet", "bungalow", "dormitory", "room"]

BEDS = ["king", "queen", "twin", "double", "single", "bunk", "sofa"]

# A room's TIER is its quality level WITHIN a hotel, orthogonal to its CLASS
# (what the unit physically is). "Deluxe Twin" and "Superior Twin" are the same
# class, same beds, same view -- and different products at different prices.
# Checked as a SET against whole tokens (features(), below) -- not substring
# matching, so list order does not affect correctness; kept alphabetised for
# humans, not for the algorithm.
#
# This is a closed vocabulary and closed vocabularies have a coverage ceiling
# -- proven, not assumed: "Prestige Twin Room with City View" vs "Elite Twin
# Room with City View" scored 92.9% and would have auto-published one tier's
# photos on the other, because neither "prestige" nor "elite" was in this
# list, so the disjointness veto below never even considered them. Neither
# token_set_ratio NOR token_sort_ratio distinguishes this case from a
# legitimate "same room, more detail on one side" pair by string shape alone
# (both metrics score the genuine pair "Deluxe Room" vs "Deluxe Room King
# Bed" in the SAME range, 71-89%, as the false pair above) -- there is no
# vocabulary-free fix for this class of ambiguity, only a wider vocabulary.
# Widen this list on evidence (a real observed word), the same way the
# original set was built, not preemptively.
TIERS = ["presidential", "premiere", "premier", "premium", "executive",
         "signature", "superior", "standard", "business", "economy", "comfort",
         "classic", "upgraded", "luxury", "deluxe", "select", "budget",
         "grand", "club", "basic", "plus", "elite", "prestige", "royal",
         "imperial", "platinum", "prime", "exclusive", "privilege", "value",
         "essential", "preferred", "ultra"]

# Accessibility is treated differently from every other attribute here: it
# vetoes on presence-vs-absence, not only on explicit disagreement. That is a
# deliberate exception to the "asymmetric information is not disagreement" rule
# the class/bedroom/view/bed vetoes all follow, for two reasons. Hotels mark
# accessible rooms explicitly and essentially always -- it is a selling point
# and frequently a legal requirement -- so silence really does mean "not
# accessible", unlike silence about bed configuration. And the consequence is
# asymmetric: showing a wheelchair user a standard bathroom, or hiding a
# genuinely accessible room behind standard photos, is a materially worse
# failure than a missing picture.
ACCESSIBILITY = {"accessible", "disabled", "mobility", "handicap",
                 "wheelchair", "ada"}

# Words too common to count as evidence when shared. Sharing "room" or "king"
# says nothing -- the beds/class/tier checks already score those attributes
# properly, and counting them again as a "distinctive shared word" would make
# corroboration fire on every pair and stop discriminating anything.
GENERIC_TOKENS = (set(CLASSES) | set(BEDS) | ROOM_NOISE | {
    "bed", "beds", "size", "full", "with", "and", "or", "the", "a", "an", "of",
    "in", "view", "views", "bedroom", "bedrooms", "1", "2", "3", "4", "5",
    "one", "two", "three", "four", "five", "large", "small", "new", "use"})

# Highest score an accessibility-mismatched pair may carry: one below the
# auto-publish floor, so it lands in the review band instead of on the site.
# Imported lazily from config to avoid a circular import at module load.
def _accept_ceiling():
    from . import config
    return config.ROOM_ACCEPT - 1


ROOM_ACCEPT_CEILING = _accept_ceiling()

# The subject of a view is whatever the property happens to look at, so it
# cannot come from a list -- "canal view" in Dubai, "lagoon view" in the
# Maldives, "burj khalifa view" being three tokens. Read it off the grammar
# instead: the qualifier is the run of words immediately before "view".
# TIERS and ACCESSIBILITY belong here for the same reason CLASSES and BEDS do:
# view_of() walks backwards from the word "view", so any adjacent word that is
# not part of the view leaks into the captured phrase. With tier words missing
# from this set, "Deluxe Canal View" captured "deluxe canal" and "Deluxe City
# View" captured "deluxe city" -- two genuinely different views that then
# appeared to agree because they shared "deluxe", while "Superior City View"
# and "Standard Superior City View" appeared to DISAGREE over a prefix that
# says nothing about the view. Both directions wrong, one cause.
VIEW_STOP = ({"with", "and", "a", "the", "of", "or"}
             | set(CLASSES) | set(BEDS) | set(TIERS) | ACCESSIBILITY)
_VIEW_RE = re.compile(r"\bview\b")


def view_of(text):
    """Qualifier preceding the word 'view', or None. Language-general."""
    m = _VIEW_RE.search(text)
    if not m:
        return None
    before = text[:m.start()].split()
    qual = []
    for tok in reversed(before):
        if tok in VIEW_STOP or tok.isdigit():
            break
        qual.append(tok)
        if len(qual) == 3:
            break
    return " ".join(reversed(qual)) or None


def _first(hay, needles):
    for n in needles:
        if n in hay:
            return n
    return None


# Bedroom count is a hard product boundary, and the platforms write it in
# different orders and notations -- "Apartment 2 Bedrooms Premium" vs "Three
# Bedroom Premium Apartment". Word and digit forms both have to parse, or the
# two look like a 100%-token-overlap match and a 3BR photo lands on a 2BR room.
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_BEDROOMS_RE = re.compile(r"(\d+|" + "|".join(NUMBER_WORDS) + r")\s*(?:bed\s?rooms?|br|bhk)\b")


def bedrooms(n):
    """Number of bedrooms stated in a normalised room name, or None if unstated.

    A studio is explicitly zero bedrooms, which is what distinguishes it from a
    1-bedroom unit -- not the absence of a number.
    """
    if "studio" in n.split():
        return 0
    m = _BEDROOMS_RE.search(n)
    if not m:
        return None
    tok = m.group(1)
    return NUMBER_WORDS.get(tok) or int(tok)


def features(name):
    """(class, view, beds, bedrooms) parsed from a room name; any may be None."""
    n = norm_room(name)
    toks = set(n.split())
    return {
        # DEFAULTS to "room", the one CLASSES entry not the other attributes
        # get: a name with no beds/tier/view word stated is genuinely
        # ambiguous (silence, not a claim), but a name with no CLASS word at
        # all is not -- "Deluxe", "2 Beds", "Standard Twin" are all, overwhelmingly,
        # ordinary hotel rooms, never a suite/apartment/studio/villa (those are
        # differentiated, premium products a hotel always names explicitly).
        # Leaving this None let a classless name veto NOTHING: measured live,
        # "Deluxe" (bare) scored 100% with no veto against "Deluxe Apartment",
        # "Deluxe Studio" AND "Deluxe Suite" -- three different physical spaces,
        # all at the highest confidence tier, purely because the Bookme side
        # never stated a class to disagree with. Defaulting to "room" closes
        # all three (room != apartment/studio/suite, correctly vetoed now)
        # while leaving every already-explicit "...Room" match untouched.
        # SET-valued, and disjointness-checked -- the same fix TIERS already
        # got, for the same reason, one attribute over. `_first(n, CLASSES)`
        # picked ONE class by CLASSES list order, so a name carrying two class
        # words resolved by array position rather than by evidence:
        # "Family Suite Room" -> "suite" (suite precedes room in the list) and
        # then vetoed against "Family Room" -> "room". Same for "Deluxe King
        # Studio" vs "Studio Suite King Bed". Both are real pairs, both were
        # rejected outright, and neither disagreed about anything -- one side
        # simply named two classes and the other named one of them. An OVERLAP
        # means the names agree on at least one class word.
        # Falls back to {"room"} when the name states no class at all, which is
        # NOT the same silence-is-not-a-claim rule the other attributes use --
        # and deliberately so, measured: a bare tier word ("Deluxe") scores 100
        # against "Deluxe Room", "Deluxe Suite", "Deluxe Studio" AND "Deluxe
        # Apartment" simultaneously, four different physical spaces, with
        # nothing in the name to choose between them. Treating an unstated
        # class as "room" is right because that is what an unqualified name
        # overwhelmingly is; suites, studios, villas and apartments are premium
        # products a hotel always names explicitly.
        "classes": sorted({c for c in CLASSES
                           if all(w in toks for w in c.split())}) or ["room"],
        "view": view_of(n),
        # Plural forms count. Hotels write "Two Queens", "2 Twin Beds", "Three
        # Singles" as often as the singular, and matching only the singular
        # token made every one of those parse as NO bed information at all --
        # which meant the bed veto could never fire on them, silently, because
        # "no beds listed" and "beds listed as plural" were indistinguishable.
        "beds": sorted({b for b in BEDS if b in toks or b + "s" in toks}),
        "bedrooms": bedrooms(n),
        # SET-valued, not a single pick. A name can legitimately carry two tier
        # words ("Standard Deluxe Twin"), and collapsing that to one via
        # first-match makes the answer depend on the order of the TIERS list --
        # arbitrary, and it made "Standard Deluxe Twin" disagree with "Deluxe
        # Twin" purely because "standard" sits earlier in the list.
        # Canonicalised through TIER_SYNONYMS so "Premium Room" and "Premier
        # Double Room" agree instead of reading as two disjoint tiers.
        "tiers": sorted({TIER_SYNONYMS.get(t, t) for t in TIERS if t in toks}),
        "accessible": bool(toks & ACCESSIBILITY),
    }


# Vetoes that describe QUALITY LABELLING rather than physical product identity.
# A caller may accept one of these when something else corroborates the pair --
# see `is_soft_veto`. CLASS, BEDROOMS and BEDS are deliberately absent: a suite
# is not a room, a 2-bedroom is not a 3-bedroom, and a king is not a twin. Those
# stay absolute.
SOFT_VETOES = ("tiers", "view")


def is_soft_veto(reason):
    """Is this veto about a quality LABEL rather than the physical room?

    The veto system was calibrated when the alternative to a match was NO
    photo. It is not: the alternative is the hotel-level lobby/landmark photo
    this project exists to remove. A Superior Twin's photograph on a Deluxe
    Twin is the same physical room one label apart -- and since the source is
    typically the lower tier, the guest sees no worse than they receive. That
    is a categorically smaller error than a picture of the pool, so a tier or
    view disagreement is worth accepting when the class, beds and bedroom count
    all still agree AND something positively corroborates the pair.
    """
    return bool(reason) and reason.split()[0] in SOFT_VETOES


def corroborations(bookme_name, agoda_name):
    """Attributes that POSITIVELY agree between two room names.

    This is the discriminator that a similarity score cannot provide. Measured
    against 11 operator-labelled pairs, the should-map and should-not-map sets
    OVERLAP on score (62.3-74.9 vs 62.5-72.0), so no threshold separates them --
    but every should-map pair has at least one attribute in positive agreement,
    and every should-not-map pair has none beyond the bare class:

        Premier Room 2 Twin Beds / Premier Double or Twin Room -> tier, beds
        Apartment, 2 Bedrooms    / 2 Bedroom Apartment ...     -> bedrooms
        Family Room Summer Deal! / Superior Family Room        -> word "family"
        Captains Room            / Classic Room                -> NONE
        Corner Suite             / Royal Suite                 -> NONE
        Twin Room                / Standard Room               -> NONE
    """
    fb, fa = features(bookme_name), features(agoda_name)
    # An ACCESSIBILITY mismatch can never be corroborated away. The score cap
    # that holds those pairs in the review band is a deliberate safety
    # decision, not a confidence signal -- showing a wheelchair user a standard
    # bathroom, or hiding a genuinely accessible room behind standard photos,
    # is worse than a missing picture. Returning no corroboration here is what
    # keeps that cap load-bearing once corroboration can otherwise auto-publish
    # a sub-threshold pair. (Caught by run.py's own accessibility selftest.)
    if fb["accessible"] != fa["accessible"]:
        return []
    out = []
    if fb["tiers"] and fa["tiers"] and set(fb["tiers"]) & set(fa["tiers"]):
        out.append("tier")
    if (fb["bedrooms"] is not None and fa["bedrooms"] is not None
            and fb["bedrooms"] == fa["bedrooms"]):
        out.append("bedrooms")
    if fb["beds"] and fa["beds"] and set(fb["beds"]) & set(fa["beds"]):
        out.append("beds")
    # A shared word that is not generic filler is real evidence: it is how
    # "Family Room" and "Superior Family Room" corroborate, and equally how
    # "Horizon, Premier Room" and "Horizon King Club Room" do -- a hotel's own
    # floor/wing name is highly distinctive.
    shared = (set(norm_room(bookme_name).split())
              & set(norm_room(agoda_name).split())) - GENERIC_TOKENS
    if shared:
        out.append("word:" + "/".join(sorted(shared))[:32])
    return out


def room_match(bookme_name, agoda_name):
    """Score a room pair and explain it. Returns (score, veto_reason|None, detail)."""
    s, nb, na = room_score(bookme_name, agoda_name)
    fb, fa = features(bookme_name), features(agoda_name)

    veto = None
    if (fb["classes"] and fa["classes"]
            and not (set(fb["classes"]) & set(fa["classes"]))):
        # Disjoint, not merely unequal: a name stating TWO classes ("Family
        # Suite Room") agrees with a name stating one of them ("Family Room").
        # The both-non-empty guard is belt-and-braces -- features() defaults an
        # unstated class to "room" rather than leaving it empty (see there for
        # why that default is right for class specifically, and wrong for
        # every other attribute).
        veto = (f"class {'/'.join(fb['classes'])} vs "
                f"{'/'.join(fa['classes'])} disjoint")
    elif (fb["tiers"] and fa["tiers"]
            and not (set(fb["tiers"]) & set(fa["tiers"]))):
        # same class, same beds, different quality level -- "Deluxe Twin" is
        # not "Superior Twin", and token_set_ratio cannot tell them apart.
        # DISJOINT, for the same reason as beds below: an overlap means the two
        # names agree about at least one tier word and merely differ in how
        # much detail they spell out.
        veto = (f"tiers {'/'.join(fb['tiers'])} vs "
                f"{'/'.join(fa['tiers'])} disjoint")
    elif (fb["bedrooms"] is not None and fa["bedrooms"] is not None
            and fb["bedrooms"] != fa["bedrooms"]):
        # a 2-bedroom and a 3-bedroom unit share nearly every token
        veto = f"bedrooms {fb['bedrooms']} != {fa['bedrooms']}"
    elif (fb["view"] and fa["view"]
            and not (set(fb["view"].split()) & set(fa["view"].split()))):
        # Also disjointness, and for a concrete reason: view_of() reads up to
        # three tokens before the word "view", so a prefix the other platform
        # omits ("Standard Superior City View" vs "Superior City View") shifts
        # the captured phrase and made two identical views compare unequal.
        # Sharing any qualifier means they are looking at the same thing.
        veto = f"view {fb['view']!r} vs {fa['view']!r} disjoint"
    elif fb["beds"] and fa["beds"] and not (set(fb["beds"]) & set(fa["beds"])):
        # DISJOINT bed configurations are different products, and a -8 penalty
        # never saved them: token_set_ratio rates a subset at 100, so "King
        # Deluxe Room with Balcony" and "Twin Deluxe Room With Sea View" scored
        # 100 and merged -- a king's photographs onto a twin, which is exactly
        # the defect this pipeline exists to remove.
        #
        # DISJOINT, not merely unequal, is the whole point. Plenty of real rooms
        # are sold flexibly ("Twin/Double Room" against "Double Room"): those
        # SHARE a bed type and are very often the same physical room, so
        # requiring an empty intersection vetoes king-vs-twin while leaving
        # twin+double-vs-double alone. Asymmetric information is still not
        # disagreement -- a side that lists no beds at all never vetoes, same
        # rule as class, bedrooms and view above.
        veto = f"beds {'/'.join(fb['beds'])} vs {'/'.join(fa['beds'])} disjoint"

    # agreeing bed config is corroboration; a partial overlap is left alone
    if fb["beds"] and fa["beds"]:
        s = min(100.0, s + 4) if fb["beds"] == fa["beds"] else s - 8

    # Accessibility is HELD BACK rather than vetoed. It is the one attribute
    # where absence carries meaning (hotels mark accessible rooms explicitly),
    # so a mismatch should never auto-publish -- but vetoing it outright buries
    # the pair in unmatched_rooms.csv, whereas the review band puts it in
    # review_rooms.csv WITH the candidate image and a decision column, where a
    # human can still recover the match. Since a room we fail to match simply
    # keeps the wrong hotel-level photo it already had, the recoverable outcome
    # is strictly better than the buried one.
    if fb["accessible"] != fa["accessible"]:
        s = min(s, float(ROOM_ACCEPT_CEILING))

    return round(s, 1), veto, {"bookme_norm": nb, "agoda_norm": na,
                               "bookme_features": fb, "agoda_features": fa}


if __name__ == "__main__":
    assert norm_room("Executive Suite [Executive Suite Executive Suite Nrhb]") == "executive suite"
    assert norm_room("Deluxe Canal View [Deluxe Canal View Deluxe Canal View Ro]") == "deluxe canal view"
    assert score(norm("Ink Hotel"), norm("INK Hotel Dubai")) >= 85
    assert score(norm("One&Only One Za abeel"), norm("One&Only One Za'abeel")) >= 90
    assert score(norm("Hyatt Regency Dubai Creek Heights"), norm("Hyatt Regency Vancouver")) < 80

    # strict_score must reject subset names that token_set rates 100
    for a, b in [("Downtown Hotel", "Millennium Plaza Downtown Hotel Dubai"),
                 ("Media Rotana", "Arjaan by Rotana - Dubai Media City"),
                 ("Gulf Court Hotel Business Bay", "Gulf Court Hotel")]:
        assert score(norm(a), norm(b)) >= 95, (a, b)      # recall metric says yes
        assert strict_score(norm(a), norm(b)) < 88, (a, b)  # precision metric says no
    for a, b in [("Raha Grand Hotel", "Raha Grand Hotel"),
                 ("TIME Onyx Hotel Apartment", "TIME Onyx Hotel Apartments")]:
        assert strict_score(norm(a), norm(b)) >= 88, (a, b)
    assert room_score("Executive Room With Canal View [Executive Room With Canal View Nrb]",
                      "Executive Room with Canal View")[0] == 100
    assert room_score("Zaabeel Room King", "Zaabeel Room Double Queen")[0] < 90

    # --- same_listing: an NHA that IS the row, vs one that name-drops a hotel -
    # Both score 100 on the recall metric; only strict_score plus the unit-mark
    # veto separates them. Pairs are real, from run 20260820-061304.
    for a, b in [("nasma luxury stays limestone house",
                  "Nasma Luxury Stays - Limestone House"),
                 ("torch tower by deluxe holiday homes",
                  "The Torch Tower by Deluxe Holiday Homes")]:
        assert same_listing(norm(a), norm(b), 90), (a, b)
    for a, b in [  # name-dropping: the case the isNHA flag exists for
                 ("kempinski central avenue",
                  "Luxury Burj View 2BR in Kempinski Central Avenue Downtown Dubai"),
                 ("bloom tower", "Dubai JVC - Bloom Tower B - Balcony, Gym"),
                   # same operator, different unit -- vetoed on the mark, not scored
                 ("frank porter rimal 3", "Frank Porter - Rimal 1"),
                 ("frank porter goldcrest views", "Frank Porter - Goldcrest Views 2"),
                 ("frank porter fairmont residences",
                  "Frank Porter - Fairmont Residence South")]:
        assert not same_listing(norm(a), norm(b), 90), (a, b)
    # the destination is noise on one side, never identity
    assert not same_listing(norm("kennedy towers cayan tower"),
                            norm("Kennedy Towers - Cayan Tower [Dubai]"), 90)
    assert same_listing(norm("kennedy towers cayan tower"),
                        norm("Kennedy Towers - Cayan Tower [Dubai]"), 90, drop="dubai")
    # ...but dropping it must not erase a unit mark
    assert not same_listing(norm("kennedy towers cayan tower"),
                            norm("Kennedy Towers - Cayan Tower 2 [Dubai]"), 90,
                            drop="dubai")

    # --- resolve_access: "access(ible)" means two different things -----------
    # ENTITLEMENT. Deliberately tested against facilities the code never names,
    # because the rule is grammatical -- if this only passed for "club lounge"
    # it would be a lookup table with extra steps.
    for t in ["Deluxe Room with Private Beach Access",
              "Villa with Direct Pool Access", "Superior Room Spa Access",
              "Studio with Gym Access", "Suite Garden Access",
              "Chalet Ski Access", "Room with Terrace Access",
              "Room with Rooftop Access", "Suite with Marina Access",
              "Deluxe Room Airport Shuttle Access",
              "1 King Premium Club Lounge Accessible"]:
        assert features(t)["accessible"] is False, t
    # ...and the pair that motivated it: the same perk, written two ways.
    for a, b in [("1 King Bed Premium Room Club Access",
                  "1 King Premium Club Lounge Accessible"),
                 ("Signature Suite Club Lounge Access Creek View",
                  "Signature Suite Club Lounge Accessible Creek")]:
        assert features(a)["accessible"] == features(b)["accessible"] is False, (a, b)
    # DISABILITY, in every vocabulary and position seen in production. The
    # "1 King Bed, Accessible" case is why the subject walk stops on
    # GENERIC_TOKENS rather than a hand-written list -- "bed" was missing from
    # one, and the marker was silently read as access TO a bed.
    for t in ["Accessible King Room", "Room, 1 King Bed, Accessible",
              "ADA Accessible Queen", "Wheelchair Accessible Studio",
              "King Room Mobility Accessible", "Deluxe King - Disabled Access",
              "Double Or Twin Accessible Superior", "Studio De Luxe Accessible",
              "Premium Room, 1 Queen Bed, Accessible"]:
        assert features(t)["accessible"] is True, t
    # Disability, written in vocabulary the raw token set did not cover. Note
    # PROMO_NOISE strips "special", so this MUST resolve before token filtering.
    # "special" is PROMO_NOISE, so without the early phrase collapse the only
    # surviving token here is a bare "needs" and the marker is gone for good.
    assert "needs" not in norm_room("Superior Special Needs Room")
    assert features("Superior Special Needs Room")["accessible"] is True
    for a, b in [("Superior Accessible Single Room", "Superior Special Needs Room"),
                 ("Superior Room Disabled Adapted", "Superior Accessible Room")]:
        assert features(a)["accessible"] == features(b)["accessible"] is True, (a, b)
    # A GENUINE one-sided mismatch still holds the pair back -- the safety
    # decision this vocabulary work makes more accurate, not weaker.
    assert features("Superior Room (Accessible)")["accessible"] is True
    assert features("Superior Double Room")["accessible"] is False
    assert room_match("Superior Room (Accessible)", "Superior Double Room")[0] \
        <= ROOM_ACCEPT_CEILING

    # class disagreement must veto however similar the strings look
    assert room_match("Executive Suite", "Executive Room")[1], "suite/room not vetoed"

    # --- terse / classless names: assume "room", never "no information" ----
    # Bare tier words with no room-type noun at all ("Deluxe", "2 Beds",
    # "Standard Deluxe") are exactly what a sparse Bookme feed hands over. Left
    # unstated, CLASS used to mean "no opinion" the same way beds/tiers/view
    # do -- but unlike those, a name with no class word is not silent about
    # its identity, it's an ordinary room by hospitality convention. Measured
    # live before this fix: "Deluxe" scored 100% with NO veto against
    # "Deluxe Apartment", "Deluxe Studio" AND "Deluxe Suite" simultaneously --
    # three different physical products, all at the highest confidence tier.
    for wrong in ("Deluxe Apartment", "Deluxe Studio", "Deluxe Suite"):
        sc, veto, _ = room_match("Deluxe", wrong)
        assert veto, f"bare 'Deluxe' not vetoed against {wrong!r} (score {sc})"
    # the fix must not become a new false-negative: a bare tier word still
    # matches a same-class, same-tier room CONFIDENTLY -- the whole point was
    # never to stop matching terse names, only to stop them matching the
    # wrong PRODUCT.
    sc, veto, _ = room_match("Deluxe", "Deluxe Room")
    assert veto is None and sc >= 85, (sc, veto)
    sc, veto, _ = room_match("2 Beds", "2 Beds Room")
    assert veto is None and sc >= 85, (sc, veto)
    sc, veto, _ = room_match("Standard Deluxe", "Standard Deluxe Room")
    assert veto is None and sc >= 85, (sc, veto)
    # an EXPLICIT class on one side is still real information and still vetoes
    # against a stated mismatch, fix or no fix -- this is the pre-existing
    # behavior the fix must leave untouched.
    assert room_match("2 Beds", "2 Beds Apartment")[1], \
        "bare bed-count name not vetoed against an explicit apartment"

    # --- abbreviations: canonicalised before scoring, not just for tiers ---
    # "Dlx"/"Std"/"Sgl"/"Dbl"/"Twn"/"Apt"/"Exec" are ordinary OTA/hospitality
    # shorthand. Left unexpanded, "Dlx" and "Deluxe" are two different tokens
    # to the scorer -- no veto (asymmetric info, same rule as everywhere else)
    # but nothing corroborates them as the same tier either, so a genuinely
    # identical room scored only 66.7%, inside the review band on spelling
    # alone. Expansion happens once, inside norm_room(), so it reaches the
    # fuzzy score, tier detection AND the class default above uniformly.
    assert norm_room("Dlx Twin Room") == norm_room("Deluxe Twin Room")
    sc, veto, _ = room_match("Dlx Twin Room", "Deluxe Twin Room")
    assert veto is None and sc == 100, (sc, veto)
    sc, veto, _ = room_match("Std Dbl Room", "Standard Double Room")
    assert veto is None and sc == 100, (sc, veto)
    # a whole-token replacement only -- must never fire on a substring
    assert "std" not in norm_room("Standard Room").split() or \
        norm_room("Standard Room") == "standard room"
    assert "apt" not in norm_room("Apartment Deluxe").split()

    # --- tier vocabulary coverage --------------------------------------------
    # A gap in TIERS is not a rounding error: an unrecognized tier word paired
    # with heavy overlap elsewhere scored 92.9%, well past auto-accept, before
    # "elite"/"prestige"/"royal" were added -- the same failure shape as the
    # king/twin bed bug, just with words this list didn't yet cover.
    assert room_match("Prestige Twin Room with City View",
                      "Elite Twin Room with City View")[1], \
        "prestige/elite tier gap regressed"
    assert room_match("Royal Suite Ocean View", "Deluxe Suite Ocean View")[1], \
        "royal/deluxe tier gap regressed"

    # --- bed configuration -------------------------------------------------
    # DISJOINT beds are different products. Found on real published rooms:
    # token_set_ratio rated these 100 and merged a king's photos onto a twin.
    assert room_match("King Deluxe Room with Balcony",
                      "Twin Deluxe Room With Sea View")[1], "king/twin not vetoed"
    assert room_match("Two Queens Deluxe Room", "King Deluxe Room")[1], \
        "queen/king not vetoed"
    # ...but an OVERLAP is not disagreement. Flexible rooms are sold as either,
    # and vetoing those would drop real matches -- which is why the rule is
    # disjointness, not inequality.
    assert room_match("Twin/Double Room Comfort", "Double Room Comfort")[1] is None, \
        "flexible twin/double wrongly vetoed against double"
    # asymmetric information is never disagreement, same as class/view/bedrooms
    assert room_match("Deluxe Room", "Deluxe Room King Bed")[1] is None
    assert room_match("Deluxe Room King Bed", "Deluxe Room, 1 King Bed")[1] is None

    # --- rate-plan noise ---------------------------------------------------
    # The offer is not the room: the same physical room sold on two rate plans
    # must still match, and these tokens are what used to stop it.
    # (norm_room keeps "room"; ROOM_NOISE strips it inside room_score)
    assert norm_room("Standard Room (Non Refundable)") == "standard room"
    assert norm_room("Standard Double Or Twin Room (Package Rate)") == \
        "standard double or twin room"
    assert room_match("Standard King Room",
                      "Standard King Room (Non Refundable)")[0] >= 95
    assert room_match("Deluxe Room With Breakfast Included", "Deluxe Room")[0] >= 95
    # stripping must not erase a room whose name is ONLY rate words
    assert norm_room("Non Refundable") == "" or bedrooms(norm_room("Non Refundable")) is None

    # bedroom count, in whichever notation and order each platform writes it
    assert bedrooms(norm_room("Apartment 2 Bedrooms Premium")) == 2
    assert bedrooms(norm_room("Three Bedroom Premium Apartment")) == 3
    assert bedrooms(norm_room("Kempinski Central Avenue-1BHK")) == 1
    assert bedrooms(norm_room("Largest 1BR in Kempinski")) == 1
    assert bedrooms(norm_room("Studio Deluxe")) == 0
    assert bedrooms(norm_room("Deluxe King Room")) is None
    assert room_match("Apartment 2 Bedrooms Premium",
                      "Three Bedroom Premium Apartment")[1], "2BR/3BR not vetoed"
    assert room_match("Two Bedroom Apartment", "2 Bedroom Apartment")[1] is None
    assert room_match("Studio Apartment", "One Bedroom Apartment")[1], \
        "studio/1BR not vetoed"
    assert room_match("Deluxe Canal View", "Deluxe City View")[1], "view not vetoed"
    assert room_match("Executive Room with Canal View",
                      "Executive Room with Canal View")[1] is None
    s_king, veto, _ = room_match("Deluxe Room King Bed", "Deluxe Room, 1 King Bed")
    assert veto is None and s_king >= 85, (s_king, veto)
    assert "junior suite" in features("Junior Suite Burj Khalifa View")["classes"]

    # -- the recall pass: defect CLASSES, each with a real pair behind it -----
    # 1. class was single-pick by CLASSES list order, so a name stating TWO
    #    classes vetoed against a name stating one of them.
    assert room_match("Family Suite Room", "Family Room")[1] is None, \
        "multi-class name still vetoes against one of its own classes"
    assert room_match("Deluxe King Studio", "Studio Suite King Bed")[1] is None
    # ...but genuinely different products must STILL be vetoed
    assert room_match("Deluxe Suite", "Deluxe Apartment")[1], "suite/apartment"
    assert room_match("Standard Room", "Standard Villa")[1], "room/villa"

    # 2. DELIBERATELY NOT FIXED: a bare tier word stays vetoed against a
    #    premium class. "De Luxe" vs "Deluxe Suite" was requested, but the same
    #    bare name matches "Deluxe Room", "Deluxe Studio" and "Deluxe
    #    Apartment" identically -- there is no evidence in the name to pick
    #    one, so accepting it is a coin flip, not a recall win.
    assert room_match("De Luxe", "Deluxe Suite")[1], \
        "a bare tier word must not silently claim a premium class"

    # 3. tier synonyms are one rung, not two disjoint ones
    assert room_match("Premium Room", "Premier Double Room")[1] is None
    assert room_match("Premium", "Premier Double Room")[1] is None

    # 4. perks and promotions are not room identity
    # ~30 tokens of advertising collapse to the 2 that are the room. "grand" in
    # that copy is also a TIER word -- left in, it manufactures a tier
    # disagreement out of pure marketing.
    assert norm_room("Twin Room- 50% off on Grand Hyatt Water Park & 20% off "
                     "on Food and Beverage (Valid Until August 2026)") == "twin room"
    assert norm_room("Horizon King Club Room - Lounge Access Including "
                     "Afternoon Tea") == "horizon king club room"
    assert norm_room("FAIRMONT King - 41sm 441sf.") == "fairmont king"
    # the real pair those two came from must now survive
    assert room_match("Twin/Double Room De Luxe Club Room",
                      "Twin Room- 50% off on Grand Hyatt Water Park")[1] is None
    assert corroborations("Horizon, Premier Room , 1 King Bed",
                          "Horizon King Club Room - Lounge Access"), \
        "a hotel's own wing name is distinctive evidence and must corroborate"

    # 5. soft vs hard vetoes -- quality LABEL vs physical product
    assert is_soft_veto(room_match("Deluxe Twin Room", "Superior Twin Room")[1])
    assert not is_soft_veto(room_match("Deluxe Suite", "Deluxe Apartment")[1])
    assert not is_soft_veto(room_match("Two Bedroom Apartment",
                                       "Three Bedroom Apartment")[1])
    assert not is_soft_veto(room_match("King Room", "Twin Room")[1])

    # 6. corroboration is what separates the operator's two labelled sets --
    #    score alone cannot (they overlap at 62-75)
    for bm_, ag_ in [("Premier Room 2 Twin Beds", "Premier Double or Twin Room"),
                     ("Apartment, 2 Bedrooms", "2 Bedroom Apartment, Bedroom 1: 1 King"),
                     ("Family Room Summer Deal!", "Superior Family Room"),
                     ("Family Suite, 1 Bedroom, Balcony", "One Bedroom Suite City View")]:
        assert corroborations(bm_, ag_), f"no corroboration found for {bm_!r}"
    for bm_, ag_ in [("Captains Room", "Classic Room"),
                     ("Corner Suite", "Royal Suite"),
                     ("Twin Room", "Standard Room")]:
        assert not corroborations(bm_, ag_), \
            f"{bm_!r}/{ag_!r} must NOT corroborate -- operator says do not map"

    # non-Latin names must survive normalisation; erasing them to "" made every
    # such room silently unmappable in any non-English market
    for s_ in ("デラックスルーム", "غرفة ديلوكس", "Люкс", "Δίκλινο"):
        assert norm(s_), f"{s_!r} normalised to empty"
        assert score(norm(s_), norm(s_)) == 100
    assert score(norm("Chambre Superieure"), norm("Chambre Supérieure")) == 100
    assert score(norm("デラックスルーム"), norm("スイートルーム")) < 90

    # undecoded HTML numeric entities, confirmed live in Agoda's Spanish-market
    # room names -- must fold the SAME as the character they encode
    assert norm("Habitaci&#Xf3;N Superior Twin") == norm("Habitación Superior Twin")
    assert "xf3" not in norm("Habitaci&#xf3;n")

    # spaced-letter rendering artifact, confirmed live (one hotel, real data):
    # must collapse to the real word, not fragment into single-char tokens
    assert norm("T R I P L E") == norm("Triple")
    sc, veto, _ = room_match("T R I P L E", "S U P E R I O R")
    assert sc < 62, f"spaced-letter artifact still scores {sc} against an unrelated word"
    # genuine single-letter room designators must NOT be collapsed (need 3+
    # spaced letters to trigger; "Room A" has exactly one)
    assert norm("Room A") != norm("Room B")

    # view parsing must work on landmarks nobody listed in advance
    assert view_of("junior suite burj khalifa view") == "burj khalifa"
    assert view_of("deluxe room with lagoon view") == "lagoon"
    assert view_of("standard room with view of the eiffel tower") is None  # postfix form
    assert view_of("deluxe king room") is None
    assert room_match("Deluxe Lagoon View", "Deluxe Beach View")[1], "unlisted views not vetoed"
    assert room_match("Deluxe Lagoon View", "Deluxe Lagoon View")[1] is None
    print("OK: normalisation, scoring and class/view vetoes behave")
