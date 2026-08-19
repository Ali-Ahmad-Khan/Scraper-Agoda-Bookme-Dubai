"""Room-name -> room category. Owns the approved taxonomy and its DB sync.

In:  a raw room name from either platform.
Out: a `v2_room_categories.name`, and (via `resolve`) that row's id.

The taxonomy is TYPE first, TIER only where a tier is a genuinely distinct
commercial product. Bed configuration and view are deliberately NOT categories:
they are per-booking attributes, they already exist as hard vetoes in match.py,
and admitting them would multiply the list combinatorially.
"""
import re

from . import match

# ---------------------------------------------------------------------------
# ORDER IS THE ALGORITHM. Room names collide constantly ("Club Deluxe Suite"
# satisfies three rules), so the first rule that matches wins and the list runs
# most-specific first. Suites outrank room tiers because a suite is a different
# product, not a better room; bedroom counts sit BELOW suites deliberately --
# "1 Bedroom Family Suite" is a small suite, not an apartment. Approved
# 2026-08-06; do not reorder without re-approving.
# ---------------------------------------------------------------------------
RULES = [
    ("Overwater Villa",        r"overwater|over water|water villa"),
    ("Villa",                  r"\bvillas?\b"),
    ("Penthouse",              r"\bpent\s?house\b"),
    ("Presidential Suite",     r"presidential|royal suite|ambassador|imperial"),
    ("Executive Suite",        r"\b(?:executive|exec|business|club)\s+suite\b"),
    ("Junior Suite",           r"\b(?:junior|jr|mini|semi)\s+suite\b"),
    ("Suite",                  r"\bsuites?\b"),
    ("Dormitory",              r"\bdorms?\b|dormitory|shared room|\bbed in\b"),
    ("Studio",                 r"\bstudios?\b"),
    # 4 bedroom-count rules are handled by _bedroom_category, not by regex --
    # see below; they sit HERE in precedence.
    ("Apartment",              r"\bapart(?:ment|hotel)s?\b|\bresidences?\b|\bflats?\b"),
    ("Chalet",                 r"\bchalets?\b|\bcabins?\b|\bcottages?\b|\blodges?\b"),
    ("Bungalow",               r"\bbungalows?\b|\bpavilions?\b"),
    ("Accessible Room",        r"accessible|disabled|mobility|handicap"),
    ("Family Room",            r"\bfamily\b|\btriple\b|\bquadruple\b|\bquad\b"),
    ("Club Room",              r"\bclubs?\b|\bclb\b|concierge|lounge access"),
    ("Executive Room",         r"\bexecutives?\b|\bexec\b"),
    ("Premium Room",           r"\bpremium\b|\bpremier\b|\bgrand\b"),
    ("Deluxe Room",            r"\bdeluxe\b|\bde luxe\b|\bdlx\b"),
    ("Superior Room",          r"\bsuperior\b"),
    ("Standard Room",          (r"\bstandard\b|\bclassic\b|\bbasic\b|\beconomy\b"
                                r"|\bbudget\b|run of house|\broh\b|\bstd\b")),
]

# Where the bedroom-count rules interrupt the regex list (immediately after
# Studio, before Apartment) -- kept as an index so RULES stays a plain table.
_BEDROOM_AT = [n for n, _ in RULES].index("Apartment")

BEDROOM_CATEGORIES = {1: "One Bedroom Apartment", 2: "Two Bedroom Apartment",
                      3: "Three Bedroom Apartment"}
MULTI_BEDROOM = "Multi Bedroom Apartment"

FALLBACK = "General"

# Every category name this module can emit, in the order they should be created.
ALL = ([n for n, _ in RULES[:_BEDROOM_AT]]
       + [BEDROOM_CATEGORIES[1], BEDROOM_CATEGORIES[2], BEDROOM_CATEGORIES[3],
          MULTI_BEDROOM]
       + [n for n, _ in RULES[_BEDROOM_AT:]]
       + [FALLBACK])

_COMPILED = [(n, re.compile(p)) for n, p in RULES]


def classify(room_name):
    """Category NAME for a room. Never None -- unmatched names get `General`,
    which is the fallback working, not the fallback failing.

    Runs on match.norm_room(), the same normalisation the matcher uses, so a
    name is classified exactly as it is matched: bedbank rate codes stripped,
    the duplicated "[Name Name Ro]" suffix collapsed, accents folded.
    """
    n = match.norm_room(room_name or "")
    if not n:
        return FALLBACK
    for i, (name, rx) in enumerate(_COMPILED):
        if i == _BEDROOM_AT:
            # bedroom count outranks the generic Apartment rule, and only
            # reaches here at all because suites/villas/studios matched first
            b = match.bedrooms(n)
            if b:
                return BEDROOM_CATEGORIES.get(b, MULTI_BEDROOM)
        if rx.search(n):
            return name
    return FALLBACK


# ------------------------------------------------------------------ DB sync
def resolve(conn):
    """{category name -> id}, creating any category that does not exist yet.

    INSERT-only, by contract: an existing row is matched case-insensitively and
    reused as-is. Nothing here ever UPDATEs or DELETEs, so `Single Deluxe` (1)
    and `Executive Suite` (2) survive untouched -- and `Executive Suite` is
    reused at id 2 rather than duplicated.
    """
    from .db import _sql  # imported here: db imports this module
    with conn.cursor() as cur:
        _sql(cur, "SELECT id, name FROM v2_room_categories")
        by_name = {r["name"].strip().lower(): r["id"] for r in cur.fetchall()}
        missing = [n for n in ALL if n.lower() not in by_name]
        for name in missing:
            _sql(cur, "INSERT INTO v2_room_categories (name, is_active, "
                      "created_at, updated_at) VALUES (%s, 1, NOW(), NOW())",
                 (name,))
            by_name[name.lower()] = cur.lastrowid
    conn.commit()
    return {n: by_name[n.lower()] for n in ALL}, missing


if __name__ == "__main__":
    # Every row of the approved proposal's worked-example table.
    cases = {
        "Standard City View": "Standard Room",
        "Deluxe Canal View": "Deluxe Room",
        "Superior Room": "Superior Room",
        "Executive Room With Canal View": "Executive Room",
        "Club Deluxe Room": "Club Room",
        "Family Room De Luxe": "Family Room",
        "Suite Regency": "Suite",
        "Suite Standard": "Suite",
        "Executive Club Suite": "Executive Suite",
        "1 Bedroom Family Suite": "Suite",
        "Studio Apartment": "Studio",
        "2 Bedroom Apartment": "Two Bedroom Apartment",
        "Zaabeel Room King": "General",
        "Siro Plus": "General",
        "Rover Room": "General",
        "Recovery Suite": "Suite",
        "Fitness Suite": "Suite",
        "Double 1 Kg Clb Acc Dlx": "Club Room",
        # precedence: type beats tier, suites beat bedroom counts
        "Club Deluxe Suite": "Suite",
        "Junior Suite Burj Khalifa View": "Junior Suite",
        "Three Bedroom Premium Apartment": "Three Bedroom Apartment",
        "Apartment 2 Bedrooms Premium": "Two Bedroom Apartment",
        "Kempinski Central Avenue-1BHK": "One Bedroom Apartment",
        "Five Bedroom Residence": "Multi Bedroom Apartment",
        "Two Bedroom Villa": "Villa",
        "Overwater Pool Villa": "Overwater Villa",
        "Presidential Penthouse": "Penthouse",
        "Bed in 6-Bed Dormitory": "Dormitory",
        "Accessible Deluxe King": "Accessible Room",
        # bedbank noise and the duplicated-name suffix must not defeat it
        "Executive Suite [Executive Suite Executive Suite Nrhb]": "Executive Suite",
        "Deluxe Canal View [Deluxe Canal View Deluxe Canal View Ro]": "Deluxe Room",
        "": "General",
    }
    for name, want in cases.items():
        got = classify(name)
        assert got == want, f"{name!r} -> {got!r}, expected {want!r}"
    assert len(ALL) == 25 and len(set(ALL)) == 25, ALL
    print(f"OK: {len(cases)} room names classified into {len(ALL)} categories")
