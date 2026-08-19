# Engineering Record — Bookme Room-Imagery Correction Pipeline

*Personal reference document. Not part of the operating docs (see `README.md`
for that) — this one is for CV / portfolio / interview use.*

---

## 1. What this is

Bookme (bookme.pk) is an OTA reselling wholesale hotel inventory. For a large
share of listed rooms, the site was showing a generic **hotel** photo instead
of a photo of the actual **room** being booked — Bookme's own supplier feed
flags this per room with a literal `AccurateMedia: false`.

I designed and built a production data pipeline that: takes a city, reads its
hotel catalogue straight from Bookme's own database, cross-references each
hotel against Agoda (a competing OTA with materially better room-level media),
resolves *which specific room* on Agoda corresponds to *which specific room*
on Bookme, downloads the real image bytes, re-hosts them on Bookme's own CDN,
and writes the corrected data back into Bookme's live database — safely,
idempotently, and resumably, against a database and object store I do not own
the schema of and am not permitted to alter.

This is not a scraper. It's a cross-platform entity-resolution and
data-correction system with hard reliability and cost constraints.

---

## Named engineering patterns — the industry vocabulary for what's below

Section 3 tells the story of *why* each decision was made. This section is
the glossary: the **standard, named pattern** each decision actually is, in
the vocabulary a hiring manager or system-design interviewer would recognise
— written assuming no prior context, each with a plain-language definition,
before saying where it's used. Every one is a real pattern with a name in the
field, not a term reached for after the fact — most predate this project by
years and have their own literature (several come from Michael Nygard's
*Release It!*, the book that named "circuit breaker" and "bulkhead" for
production resilience engineering, and from Microsoft's *Cloud Design
Patterns* catalogue, an industry-standard reference).

### Storage & data patterns

- **Content-addressable storage (CAS).** A storage scheme where an object's
  key *is* the hash of its own bytes, instead of an arbitrary name a caller
  chooses. This is the exact architecture behind Git's object database,
  Docker's image layers, and IPFS — the same content always lands at the
  same address, so storing it twice is a no-op, not a duplicate. Built here
  for every image: `key = md5(image_bytes)`. Consequence, not accident: a
  crashed run's half-finished upload is never cleaned up, because the next
  attempt computes the identical key and finds it already there —
  idempotency *falls out of* the storage design instead of needing separate
  retry logic to provide it.
- **Idempotent upsert (write-once register).** An update that can run any
  number of times and only ever has an effect the *first* time it applies.
  Standard in ETL and data-sync systems (the SQL-standard form is `INSERT ...
  ON CONFLICT DO NOTHING` / `UPSERT`). Built here as `SET col = COALESCE(col,
  %s)` — a column starts `NULL` and can be filled exactly once; every
  subsequent call with a new value is provably a no-op, because
  `COALESCE(existing_value, anything)` is definitionally just
  `existing_value`. This is what lets a re-run backfill a missing field
  without any risk of clobbering one already set — the guarantee is a
  property of the SQL itself, not of remembering to check first.
- **Event sourcing / append-only log (write-ahead log).** Instead of storing
  *current state* and overwriting it, store every *event* as an immutable
  line appended to a log, and derive current state by reading it back.
  Standard in databases' own crash-recovery logs (the WAL every production
  RDBMS keeps) and in event-driven architectures. Built here as the
  operational ledger: every publish and every failure is one appended CSV
  row, never rewritten in place.
- **Last-write-wins (LWW) conflict resolution.** When the same key gets
  written more than once, the most recent write is the one that counts on
  read — a named, standard conflict-resolution strategy in distributed
  databases (Cassandra, DynamoDB, and CRDT "LWW-register" types all document
  it explicitly). Applied when reading the ledger back: the latest row per
  hotel id is authoritative, so a hotel that failed and later succeeded
  correctly stops showing as failed, without ever rewriting the earlier row.
- **Master data management / entity resolution / record linkage.** The
  named subfield (with its own conferences and vendor tooling — Master Data
  Management is a real job title) covering exactly this problem: matching
  records that describe the same real-world thing across two systems that
  share **no common key**. That is the core problem this whole project
  solves — a Bookme hotel and an Agoda hotel, or a Bookme room and an Agoda
  room, have no shared id, only fuzzy, independently-observed evidence
  (name, coordinates, physical attributes) to link them by.
- **Check-before-fetch (avoiding redundant I/O).** Reordering a cheap local
  check ahead of an expensive remote one, rather than doing the expensive
  work first and discarding the result if the cheap check would have said no
  — the same principle behind an HTTP cache's `If-None-Match` or a build
  system's up-to-date check before recompiling. Found by a dedicated audit
  for exactly this class of defect: a room's images were being downloaded
  from a third-party CDN and re-uploaded to object storage *before* checking
  whether the database already had that room's image — the check existed,
  it just ran after the expensive work instead of before it. Reordering it
  cut a measured re-run from 577 unnecessary network transfers to 46, with
  the published result byte-for-byte identical.

### Concurrency & distributed-systems patterns

- **Check-then-act race condition.** A bug class where code checks a
  condition, then acts on it, but another thread can change the world in the
  gap between the two — the check is stale by the time the act happens.
  Found and fixed in this project's own concurrent image-mirroring code: an
  in-memory "already uploaded" cache was being marked complete *before* the
  upload was confirmed durable, so a second thread checking that same cache
  could receive a public URL for an object that did not exist yet — a race
  between "is this marked done" and "is this actually done." Fixed by
  moving the mark strictly after a provable write, and proved fixed with a
  genuine concurrent stress test (8 threads racing one real URL against
  production object storage), not just code review.
- **Mutual exclusion (mutex) via an advisory lock.** A mechanism that lets
  only one process at a time enter a protected section of code — the
  fundamental primitive of concurrent programming. No unique database
  constraint existed to stop two runs of this pipeline writing the same
  hotel's rooms twice (confirmed directly against the schema), so an
  OS-level advisory file lock (`flock`) was used as an **application-level
  mutex** substituting for a missing database-level one — one process holds
  it for its whole lifetime, a second is refused outright, and the kernel
  releases it automatically on any exit including a crash, so it can never
  deadlock a future run.
- **Bulkhead pattern.** Named after a ship's bulkheads — physical
  compartments that stop one flooded section sinking the whole vessel.
  Applied in resilience engineering to mean: bound how much of a shared
  resource one piece of work can consume, so it can't starve everything
  else. Applied here as a fixed-size worker pool (`ThreadPoolExecutor`,
  8 workers) for concurrent image downloads — bounded concurrency, not
  unbounded, so one hotel's image batch can't exhaust connections or memory.
- **Commutative, idempotent merge (a mergeable, order-independent union).**
  A combining operation is safe to apply in any order, and safe to apply
  twice, exactly when it's commutative (order doesn't matter) and idempotent
  (repeating it changes nothing) — the same mathematical property that makes
  CRDTs (conflict-free replicated data types) safe for eventual consistency
  without coordination. Room names gathered across multiple, independently
  timed search probes are combined by set union keyed on room name — provably
  safe to run the probes in any order, or run some of them twice, and get the
  identical result either way.

### Resilience & fault-tolerance patterns

- **Circuit breaker.** After a dependency fails repeatedly, *stop calling
  it* for a cooldown period instead of continuing to hammer (and worsen) a
  failing service — the standard pattern from production resilience
  engineering (it's literally named "Circuit Breaker" in Michael Nygard's
  *Release It!* and in Microsoft's Cloud Design Patterns catalogue). Applied
  to the Agoda client: after 6 consecutive throttled responses, the pipeline
  stops issuing requests for a fixed cooldown before resuming, rather than
  retrying into an active block.
- **Client-side rate limiting.** Deliberately pacing outgoing requests below
  a dependency's real tolerance, instead of finding the limit by getting
  blocked. Applied as a hard minimum interval (1.5s) enforced between any two
  Agoda requests, process-wide — a number arrived at by measurement (a burst
  run got HTTP 502s from hotel 194 onward, with the block outliving the run),
  not guessed.
- **Retry pattern / transient fault handling, with exponential backoff.** The
  standard cloud-design response to a fault that is *expected to be
  temporary* (a network blip, a dropped connection) — catch the specific
  transient error, recover the resource, retry with a growing delay between
  attempts, and only then treat it as a real failure. Applied to every
  network-touching call in the system: MySQL connections (up to 6 attempts,
  backing off from 2s to 64s — roughly two minutes total, sized for an
  ordinary router reboot or database failover), and every Bookme/Agoda/COS
  call, each retrying transport failures independently while treating an
  explicit "not available" answer from the service as the permanent fact it
  is, never retried.
- **Cascading failure, caught and fixed before shipping.** A single point of
  recovery that itself has no retry budget is not recovery, it's a delayed
  single point of failure — a well-documented anti-pattern (Google's SRE
  book calls the shape "retry storms and cascading failures" from the other
  direction; this is its quieter, equally damaging sibling: a recovery
  attempt with *no* retry at all). Found here: the original MySQL-reconnect
  logic tried exactly once, and if that single attempt also failed — the
  normal case, since it lands inside the same outage that broke the
  original connection — the process kept using a connection object bound to
  a closed socket for every hotel processed afterward, turning one transient
  blip into a failure of the entire remainder of a multi-hour run. Fixed by
  giving the reconnect itself a retry budget, and by guaranteeing the
  function can never hand back the broken object it was asked to replace —
  verified with a test that asserts the *identity* of the returned
  connection, not just that a call returns "successfully."
- **Graceful shutdown (signal handling).** Distinguishing the signal a
  human's interrupt sends (SIGINT) from the signal an *orchestrator* sends
  when deliberately stopping a service (SIGTERM — what `systemctl stop`,
  `docker stop`, and a cloud instance's shutdown sequence actually send),
  and treating both as "finish the current unit of work and exit cleanly"
  rather than letting the unhandled signal kill the process mid-transaction.
  This is the standard contract every container orchestrator and process
  supervisor expects a well-behaved service to honour. Verified live, not
  just read from the code: a real running process was sent a real `SIGTERM`,
  confirmed to finish its in-flight work, write its progress, and exit with
  status 0 — then a second process was started and confirmed to resume
  correctly from what the first one left behind.
- **Checkpointing (crash-recovery via persisted progress).** Recording
  completed units of work to durable storage *as they finish*, so a process
  that dies and restarts resumes from the last checkpoint instead of
  repeating everything from zero — the same idea behind a database's
  write-ahead log or a long-running batch job's save-points. Applied to the
  per-hotel room-name harvest (which can run for tens of minutes on a full
  city): every completed (hotel × search-shape) unit is written to disk
  immediately, keyed by a fingerprint of exactly which hotels and which
  search parameters it covers — so a checkpoint from one city's run can
  never be silently reused by a different city's run and reported as
  "already checked" work that was in fact never done. Verified live: a real
  run was interrupted a third of the way through, restarted, and confirmed
  to skip only the genuinely-completed portion.
- **Write-ahead durability (fsync).** The difference between "the operating
  system has the bytes" and "the bytes are physically on disk and will
  survive a power cut" — `write()` alone only guarantees the former.
  Applied to the local record of "this hotel is done": every append now
  calls the OS-level `fsync`, and the guarantee was verified by having a
  completely separate process read the file immediately after the write
  call returned, rather than trusting the writer's own belief that the data
  is safe.
- **Graceful degradation.** Continuing to deliver a reduced but genuinely
  useful result when a non-essential dependency is unavailable, rather than
  failing the whole operation. Applied when Bookme has no live search
  presence for a city at all, or when its partner-API credentials fail
  outright: the pipeline logs it plainly and continues using Agoda alone,
  rather than aborting a run that could still deliver every hotel's images.

### Algorithms & correctness patterns

- **Greedy algorithm for the (weighted) bipartite matching / assignment
  problem.** A classical combinatorial optimisation problem: two disjoint
  groups of items, a compatibility/quality score for each possible pairing
  across the groups, find an assignment where nothing is claimed twice. The
  textbook optimal solution is the Hungarian algorithm (O(n³)); this project
  uses the standard simpler alternative — sort every viable pair by score,
  strongest first, and assign greedily, skipping anything already claimed —
  a deliberate, named engineering tradeoff (near-optimal, deterministic,
  and easy to explain to a non-engineer, over provably-optimal and opaque)
  for a case where "explainable and correct" matters more than "provably
  maximal." This is literally what matches a Bookme room to an Agoda room:
  the two room lists are the two groups, `room_match()`'s score is the edge
  weight, and no room may be claimed twice.
- **Constraint satisfaction over an objective function (hard constraints vs.
  a soft score).** A pattern from optimisation and AI: separate "how good is
  this candidate" (a continuous score to *maximise*) from "is this candidate
  even legal" (boolean rules that *eliminate* a candidate outright,
  regardless of its score). Conflating the two — expressing a hard rule as
  merely a scoring penalty — lets a high enough score on unrelated
  dimensions overrule a rule that should have been absolute. Applied
  throughout the room matcher: fuzzy string similarity is the soft
  objective; room class, bedroom count, and (after this session's fixes)
  disjoint bed/tier/view are hard constraints that veto a pairing regardless
  of how high the string similarity scores.
- **Property-based testing.** Testing an *invariant that must hold for every
  input*, rather than a fixed set of hand-picked example inputs — the
  input space itself is sampled or permuted at test time. Applied to the
  room-assignment algorithm: its output must be identical regardless of the
  order either platform lists its rooms in, verified by shuffling both
  lists 60 times per run and demanding byte-identical output every time,
  not just checking two orderings someone thought of.
- **Mutation testing.** Verifying that a test suite actually *has teeth* by
  deliberately reintroducing a known bug into the code and confirming the
  test suite fails — a test that cannot fail against known-bad code is
  worthless regardless of how thorough it looks. Applied to the
  property-based test above: the old, order-dependent assignment logic was
  deliberately reinstated, and 46 of 60 randomised runs correctly failed,
  proving the property test has real detection power rather than just
  existing.

### Security pattern

- **Positive security model (allow-listing) as defense in depth.** Two
  independent named ideas working together: an allow-list refuses everything
  by default and only permits an explicit, enumerated set of safe operations
  (the opposite of a deny-list, which tries to enumerate every *bad* thing —
  structurally incomplete, since you can't list what you haven't thought of
  yet). Defense in depth means stacking independent layers of protection so
  a failure in one doesn't remove all protection. Applied together: the
  database credential holds full write privileges (layer one, outside this
  project's control), and every SQL statement this project issues is
  additionally routed through a single choke point that inspects the verb
  and refuses anything that isn't `SELECT`/`INSERT`/`SHOW` — a second,
  independent, code-level layer that holds even if the grant is ever
  widened by someone else, live-tested by firing `UPDATE`/`DELETE`/`ALTER`/
  `TRUNCATE` directly at it and confirming every one is rejected before
  reaching the server.

---

## 2. System architecture

```
 operator (CLI/wizard)
        │  city name or id
        ▼
 ┌──────────────┐   read-only, zero cost      ┌────────────────────┐
 │  MySQL 8.0    │ ───────────────────────────▶│ hotel identity:     │
 │ (bookme_sky_  │   v2_common_hotels           │ name, slug, geo,    │
 │  uat, txsql)  │                              │ address, city       │
 └──────────────┘                              └─────────┬───────────┘
                                                            │
                       ┌────────────────────────────────────┼─────────────────┐
                       ▼                                    ▼
             ┌──────────────────┐                 ┌──────────────────────┐
             │ Bookme partner API│                 │ Agoda (suggest +     │
             │ (optional, per-   │                 │ room-grid HTTP API,  │
             │ hotel by slug,    │                 │ Playwright browser   │
             │ live supplier     │                 │ fallback for the ~⅓  │
             │ ROOM NAMES)       │                 │ of properties whose  │
             └─────────┬─────────┘                 │ grid is JS-gated)    │
                       │                            └──────────┬────────────┘
                       └───────────────┬────────────────────────┘
                                       ▼
                       ┌───────────────────────────────┐
                       │  entity resolution engine:     │
                       │  name normalisation (Unicode,  │
                       │  multi-script) + geo-verify +   │
                       │  hard veto rules (class/beds/   │
                       │  view) + room taxonomy classify │
                       └───────────────┬─────────────────┘
                                       ▼
                       ┌───────────────────────────────┐
                       │  per-hotel, ONE transaction:    │
                       │  download bytes → COS (content- │
                       │  addressed) → INSERT v2_rooms +  │
                       │  v2_attachments → COMMIT →       │
                       │  ledger                          │
                       └───────────────┬─────────────────┘
                                       ▼
                    live site now renders the correct room photo
```

---

## 3. Key engineering decisions (the depth is in the *why*)

### 3.1 Identity vs. state — the load-bearing data-modeling distinction

Every fact this system touches was explicitly classified as either **identity**
(what a room *is* — its name, category, images; stable, safe to union across
multiple observations) or **state** (what's *currently true* — availability,
price; true at an instant, and merging observations from different moments
would fabricate a reality that never existed).

This governed two major design calls:
- Room **names**, gathered across multiple search "probes" (different dates,
  different occupancy), are safely **unioned** — a room seen on any one probe
  is a real room. Doing the same with availability or price would have been a
  correctness bug, not an optimisation.
- Room **categories** are computed once, deterministically, from the name —
  never inferred from price rank, because price ordering is state and category
  is identity. (Considered and explicitly rejected inferring a brand-name
  room's tier from its price position — would have been a silent domain-truth
  violation.)

### 3.2 Cross-platform entity resolution without a shared key

Neither hotel nor room has a common identifier across Bookme and Agoda. Built
a resolution ladder instead of trusting any single signal:

- **Hotel matching**: name similarity (fuzzy token-set scoring) proposes
  candidates → each candidate's coordinates are geo-verified against the
  authoritative DB coordinates → Agoda's `isNHA` flag filters out individually
  listed vacation-rental units that share a building (and therefore
  coordinates) with the real hotel and can score 100% on name.
  *Measured failure mode this catches*: a name-alike hotel 5,738 km away
  (Dubai vs. Málaga) scoring 81% on loose similarity — rejected correctly by
  the strict geo/precision gate, confirmed live.
- **Room matching**: fuzzy score on normalised names, with **hard vetoes** —
  not scoring penalties — on room class, bedroom count, and view, because
  "Executive Suite" vs. "Executive Room" scores ~90% on any string metric but
  is a different product; a scoring penalty is the wrong tool for a boolean
  domain fact.
- **Name normalisation is Unicode-aware and multi-script** (NFKD accent
  folding, preserves non-Latin scripts) after finding that a naive
  `[^a-z0-9]` filter silently erased every Japanese/Arabic/Thai/Cyrillic name
  to an empty string, making every such room unmappable with no error raised
  anywhere — a correctness bug for a system meant to retarget to any city.

### 3.3 Cost engineering — measured, not assumed

- **Recognised a whole search phase was redundant** once DB access was
  granted: hotel identity used to cost one network search per hotel (or per
  batch); it now costs zero, because the DB already has it.
- **Then eliminated the other network-search phase too, later, by finding a
  cheaper endpoint the described integration didn't mention.** Bookme's
  room-name lookup used to cost a whole city-wide polling search per probe
  (~13 minutes / ~24 rounds) regardless of whether 1 or 1,340 hotels were
  being processed — a fixed, city-scoped cost, made an explicit, printed,
  opt-in decision rather than a hidden default while it was still
  unavoidable. Investigated a colleague-suggested partner endpoint rather
  than accepting the description at face value, found the description
  itself pointed at the wrong deployment environment (production API against
  a UAT database — a live, self-caught methodology error, not a hunch),
  corrected it, and confirmed the database's own slug field works as a
  **direct, per-hotel key** with no search step at all (68/68 exact match).
  Turned a fixed per-city cost into a per-hotel one: **+25% more rooms in
  2.7× less time** on a live 5-hotel A/B, and a targeted few-hotel re-run
  dropped from ~6.5 minutes to ~6 seconds.
- **Probe strategy tuned by live experiment, not intuition, with the number
  recorded next to the decision**: a weekend check-in one cycle out beat an
  arbitrary +45-day probe on 4 of 5 test hotels (one hotel went from 0 to 14
  visible rooms). Occupancy=2 outperformed occupancy=1 — the *opposite* of the
  naive assumption that a single guest would see more inventory — because
  double/twin rooms drop out of supplier feeds when priced for one guest
  (measured: 46 vs. 29 rooms, same hotel, same night).
- **Escalation is bounded, not unbounded.** Thin results widen through a fixed
  ladder (later weekends → longer stay → midweek → single occupancy),
  unioning every rung — proven live that stopping early is unsafe (one hotel's
  7 rungs converged on an identical result, which looks the same as a hotel
  where a later rung *would* have found something different).
- **API response caching**, keyed by property + probe, avoiding a ~800 KB
  re-fetch for every re-verification of a candidate already checked.

### 3.4a Concurrency — parallelised exactly what was safe to, nothing else

Asked myself, explicitly: what's running serially here that doesn't *need*
to be, as distinct from network pacing that legitimately has to stay serial?
Answer required reading the actual rate-limiter, not guessing:

- Agoda's page/search API is deliberately, globally throttled
  (`MIN_INTERVAL = 1.5s` between *any* two requests, process-wide state) —
  confirmed live that bursting it gets you blocked (a 465-hotel run at full
  speed got 502s from hotel 194 onward, block outliving the run). That stays
  exactly as it is; threading around it wouldn't even help, since the pacer's
  state is shared regardless of thread count.
- Image mirroring is a *different service pair* entirely (source: Agoda's/
  bstatic's CDN; destination: Tencent COS) — neither is the paced endpoint,
  neither is rate-limited the way the page API is, so fetching a hotel's
  images one at a time was pure unrewarded serial wall-clock time.
  **Parallelised it** with a bounded thread pool (8 workers): a hotel with 5
  rooms × 6 images went from 30 sequential download+upload round trips to up
  to 8 concurrent ones — order preserved per room via positional
  reassociation of results, not arrival order, proven with a real timing
  assertion in the test suite (6 images complete in ~55ms against a
  sequential floor of 300ms+), not just "the code runs."
- **Caught a real concurrency bug in my own fix, before it shipped**: the
  first version marked an uploaded image as "done" before the upload had
  actually completed, so two threads racing on the identical image URL could
  hand back a public URL for an object that didn't exist yet. Found by
  tracing the exact sequencing, fixed by moving the "done" marker to after a
  provable upload, and left a live regression test — 8 threads race a single
  real URL against production object storage, every returned URL must be
  immediately fetchable.

### 3.4 Reliability engineering

A dedicated pass asked, explicitly, the question a production incident
review asks after the fact: what happens if the internet drops for 30
minutes, drops repeatedly, the server itself restarts, or MySQL and object
storage both fail mid-run — *before* any of that happens live, not after.

- **Crash unit = one hotel.** Rooms and their image attachments commit in a
  single DB transaction; the local ledger is written only after that commit
  succeeds. A crash between commit and ledger write causes *repeated work*
  (safe), never *corrupted data* — verified by the DB-level name dedupe as a
  second, independent safety net.
- **Idempotent object storage**: every image is keyed by the MD5 hash of its
  own bytes. A re-run of any hotel reuses the same key for the same picture —
  no duplicate uploads, no orphaned objects that matter (an orphan from a
  crashed run is silently reused, not cleaned up, because it costs nothing to
  leave).
- **Found and fixed a real cascading-failure bug in my own first-draft
  reconnect logic**, before it shipped, by asking "what if the retry itself
  fails?" — a question the first version never answered. A dropped MySQL
  connection (found live, not hypothesised: a real `Connection reset by peer`
  hit mid-run) was originally handled by a *single* reconnect attempt; since
  that attempt lands inside the same outage that broke the original
  connection, it usually fails too — and the failure mode was silent: the
  process kept using a connection object bound to a closed socket for every
  hotel processed afterward, turning one transient blip into a failure of an
  entire multi-hour run. Rebuilt as a proper retry-with-backoff (up to 6
  attempts, ~2 minutes total), applied to every database read the pipeline
  performs, not just the write path, and proved the fix with a test that
  asserts the *identity* of the returned connection object — not just that a
  call "succeeds," because the original bug's call *also* appeared to succeed
  at first glance.
- **Graceful shutdown, verified live against a real signal, not read from the
  code.** Distinguished the signal an orchestrator sends when deliberately
  stopping a service (`SIGTERM` — what `systemctl stop`/`docker stop`/a cloud
  shutdown actually send) from a developer's `Ctrl-C` (`SIGINT`); the former
  was completely unhandled before this pass, an instant kill mid-transaction.
  Sent a real `SIGTERM` to a real running process, confirmed it finished its
  in-flight work and checkpointed cleanly, then confirmed a second process
  resumed correctly from exactly where the first left off.
- **Checkpointing added to the longest-running unattended stage** (the
  per-hotel room-name harvest, which can run tens of minutes on a full city):
  every completed unit of work is durably recorded as it finishes. Caught my
  own bug while building this, before it shipped: the first version had no
  scope key, so a checkpoint from an interrupted Dubai run could be silently
  loaded by a later Vienna run and cause it to skip search shapes it had
  never actually asked — the exact "unearned zero" failure class this whole
  project exists to eliminate, reappearing through the crash-recovery
  mechanism instead of the network. Fixed by fingerprinting the checkpoint to
  the exact hotel set and search parameters it covers, with a dedicated test
  proving a checkpoint from one scope contributes nothing when loaded against
  another.
- **Write-ahead durability, verified from outside the writing process.** The
  local record of "this hotel is done" now calls the OS-level `fsync` on
  every write, and the guarantee was checked by having a *separate* Python
  process read the file immediately after the write call returned — proving
  the bytes are on disk, not just trusting that they are.
- **A subtle escalation-loop bug**, found via a formal domain-truth audit
  after a suspicious log line ("0 rooms" for a hotel that plausibly has rooms
  somewhere): a probe loop was breaking early on a *global* zero, which could
  mask a *specific* hotel that a later probe would have resolved. This
  directly contradicted an invariant the codebase enforces everywhere else
  ("never stop at first result") — found the exception to its own rule and
  closed it. Later generalised further, by explicit direction: removed the
  room-count *threshold* that gated escalation entirely, on both platforms —
  measured that unioning more search shapes kept finding more rooms well past
  the old cutoff (+46% over the best single shape, still not plateaued), so
  "probably enough" was replaced with "every shape, always, for every hotel."

### 3.4b Wasted-work audit — found and fixed real, measured redundant I/O

A separate, dedicated pass asked a different question than reliability:
*not* "does this survive a failure," but "does this system already have an
answer somewhere and pay to get it again anyway." Passing tests don't catch
this class of defect — the system was working correctly, just wastefully.

- **The main finding**: a room's candidate images were downloaded from
  Agoda's CDN and re-uploaded to Tencent COS *before* checking whether the
  database already had that room's image — the check existed (it had to,
  the write path is idempotent and correctly skips an already-complete room)
  but ran *after* the expensive network round trip instead of before it, so
  it only ever decided what to throw away, never what to skip doing.
  Traced the exact data flow end to end to confirm the "already has an
  image" fact was genuinely available earlier, not something that would
  need a new query to produce — it was the identical query the write path
  was already going to run, just called at the wrong point in the sequence.
  Measured, not estimated: a re-run of two mostly-complete hotels moved from
  **577 image transfers to backfill 2 rooms down to 46 image transfers to
  backfill 8 rooms** — zero change in what gets published, real reduction
  in work.
- **A second, smaller finding in the same pass**: the same city's full
  hotel-id list was being queried from the database twice, a few lines
  apart, because one query's fully-fetched result set was narrowed down to
  a smaller shape and returned, discarding the broader set it had already
  paid to fetch — and a second query then re-fetched exactly that discarded
  broader set for an unrelated summary line. Merged into one query producing
  both shapes.
- **Equally deliberate: what was found and explicitly left alone.** Two
  probe-escalation ladders were flagged as candidate waste by pattern-match
  alone (an expensive path running unconditionally, a classic
  expensive-before-cheap smell) but confirmed, on inspection, to be a
  correctness decision, not an oversight — removing the "thin hotel"
  threshold that used to gate them was an explicit prior fix (§3.4 above),
  made *because* the threshold was silently dropping real rooms. Reporting
  that as newly-found "waste" and re-adding a gate would have reintroduced
  the exact bug it replaced. The audit's own standard — correctness
  outranks savings, always — held here, not just in principle.

### 3.5 Safety, by construction, not by convention

- The database user holds `ALL PRIVILEGES` on the schema. Every SQL statement
  the pipeline issues is routed through a single choke point that inspects the
  verb and **refuses anything that is not `SELECT`/`INSERT`/`SHOW` at
  runtime** — so "additive only" is enforced by the code, not by trusting
  every future edit to remember a rule. Proven with a live test that fires
  `UPDATE`/`DELETE`/`ALTER`/`TRUNCATE` at the guard and confirms each is
  rejected before reaching the server.
- Credentials loaded from an environment file that was found world-readable
  and locked to owner-only on discovery.
- Every module ships its own runnable self-check (`python -m pipeline.<name>`)
  — assert-based, no framework — so a change to matching, categorisation, or
  the write path fails loudly and immediately rather than shipping a silent
  regression.

### 3.6 A 25-category room taxonomy, designed for generalisation

Faced with an open-ended set of real-world room names across an entire hotel
catalogue (no fixed vocabulary), designed a **precedence-ordered rule table**
(type before tier, suites before generic room tiers, explicit bedroom counts
before suite-vs-apartment ambiguity) rather than a flat keyword match, because
names collide constantly ("Club Deluxe Suite" satisfies three naive rules).
Brand-name rooms with no generic tier word ("Zaabeel Room", "Rover Room") are
deliberately left to a `General` fallback rather than guessed at by price rank
— an explicit identity-vs-state call, not an oversight. Validated against 32
real room-name test cases before being wired to the database, and the
category table itself is created idempotently, reusing pre-existing rows by
name rather than duplicating them.

### 3.7a Validated at scale, in a second and third market, not just where it was built

Everything above was originally tuned on one city (Dubai). Rather than assume
it generalised, ran a structured validation campaign — 50 hotels across 10
countries spanning 4 scripts and naming conventions (Austria, Saudi Arabia,
Azerbaijan, Bangladesh, Armenia, then Brazil, Albania, Bosnia, Peru, South
Africa) — specifically hunting for the class of bug that only a *different*
market would expose. It found four, all fixed at the root rather than patched
for the instance:

- **A UAE-only address-parsing rule silently degraded every non-UAE hotel.**
  Measured before touching it: across 4,000 real catalogue addresses, 93%
  carry a 4+ digit postcode and 90% end with the country — but field order is
  **not universal** (`"vardanants 15/4, 0010, armenia, yerevan"` puts the city
  *last*). The original truncate-at-postcode logic left **~4,400 hotels
  (~5% of the catalogue)** with a fallback query too short to use at all —
  silently dead for entire address styles. Redesigned around **removal**
  instead of **truncation** — order-independent by construction, so no field
  position can cost the locality token — and separately discovered that
  blanket-removing a country name breaks cities that embed it (**"Panama
  City, Panama"** → a bare `"City"`) or *are* it (**8,613 hotels, ~10% of the
  catalogue**, sit in cities whose name equals their country — this DB stores
  several whole countries as single cities).
- **A hardcoded fallback country code was reached by a second, independent
  path I hadn't considered**: trusting a name-based destination resolver.
  Observed live, unprompted: "Albanien" (German for Albania) resolved to
  **Switzerland**; "Rodriguez De Mendoza" (Peru) resolved to **Mexico**. A
  wrong country code doesn't error — it silently builds a URL that never
  lands and reports a real hotel as roomless. Closed by sourcing the country
  code from the database exclusively, with an explicit cross-check that logs
  a warning whenever the two independent sources disagree (both real
  disagreements above were caught this way, in production, the same day the
  fix shipped).
- **A single mis-encoded character class**, only visible outside English-
  language markets: undecoded HTML numeric entities in Agoda's Spanish-market
  payloads (`"Habitaci&#xf3;n"` for `"Habitación"`) were silently torn into
  meaningless tokens by the existing Unicode-safe normaliser, degrading match
  quality for an entire language rather than raising any error.
- **A rendering artifact scoring as a real match**: one hotel's letter-spaced
  room names (`"T R I P L E"`) scored 77.8% against an unrelated word
  (`"S U P E R I O R"`) because the fuzzy-matching library treats a pile of
  single-character tokens as strong evidence regardless of origin. Measured
  prevalence first (3 rooms, one hotel — not systemic) before choosing a
  general character-pattern fix over a blocklist of the specific words
  observed, so the fix covers the *class* of artifact, not the instance.

### 3.7b Self-auditing findings for overfitting, and proving the difference with a controlled experiment

Challenged directly on whether prior fixes were root-cause treatments or
just patches shaped around the reported case. Audited every one against
inputs never previously run — and confirmed **5 of 5 were genuinely
overfitted**, including a unit test that had been written to match the
buggy output and therefore certified the bug as correct behaviour. Concrete
example of the failure mode caught: a test asserted an address-parser
dropped a city name and called that "passing."

Rebuilt the test suite from **examples to properties** as a direct result.
Order-independence (a room-assignment algorithm must produce identical
output regardless of which order either platform lists its rooms in) is
verified by shuffling both input lists 60 times and demanding byte-identical
output — not two hand-picked orderings. Then **mutation-tested the property
itself**, to prove it wasn't decoration: deliberately reintroduced the old,
order-dependent logic and confirmed the property test catches it — 46 of 60
random shuffles diverged, isolated from every other assertion in the suite.
A property test that cannot fail against a known-bad implementation is
worthless; this one was proven not to be.

### 3.7c A precision/recall tradeoff calibrated deliberately, measured in both directions

Directed explicitly to bias toward recall — in this domain a missed match
costs more than a near-miss, because a room the pipeline fails to match
keeps the *wrong* photo it already had, while a near-miss at least shows a
real room from the right hotel. Rather than tune one metric, measured both,
before and after a redesign of the veto system:

| | before | after |
|---|---|---|
| genuine same-room name variants that still match (**recall**) | — | **100.0%** (0 lost, over 2,521 synthetic pairs built from real published room names) |
| distinct rooms within one hotel correctly told apart (**precision**) | 30.3% | **55.8%** |

Found the root cause of the precision gap by decomposing every false-accept
pair into the tokens present on only one side — the *evidence being
discarded* — rather than guessing at new rules. That surfaced three
independent bugs sharing one shape: bed configuration, quality tier, and
view were each vetoing on **inequality** when the correct rule (found only
after two of the three initially cost real recall and had to be corrected)
is **disjointness** — an overlap between two attribute sets means the names
agree on something and merely differ in how much detail one side spells
out, and vetoing that trades a real match for imaginary safety. Also found:
a room's declared bed configuration parsed **only its singular form**, so
"Two Queens" was silently indistinguishable from "no beds stated at all,"
which meant the bed veto could never fire on any plural-form room name —
invisible without checking coverage, not merely accuracy.

### 3.7d Concurrent-write safety — the highest-severity finding of the whole project

A full audit for production-readiness (explicitly scoped: failure modes,
wasted work, time/space complexity, silent degradation) surfaced the single
most severe gap found in this project: **the table this pipeline writes to
has no unique constraint** — verified directly against the schema, not
assumed — so the only thing preventing two concurrent runs from duplicating
every room in a hotel is a plain read-then-insert check, which is atomic
against itself inside one transaction but **not against a second process**.
And the damage would have been **permanent**: cleaning up a duplicate needs a
`DELETE`, which this pipeline is contractually forbidden from ever issuing,
so there would have been no in-system recovery path at all.

Closed with an advisory OS-level file lock (`flock`) acquired before any
write path and held for the process lifetime — released automatically by the
kernel on any exit, including an unclean kill, so a crashed run never leaves
a stale lock requiring manual intervention. Proved live with a genuine
two-process test rather than reasoning about it: process A acquires and
holds → process B is refused immediately → A releases → a fresh process C
acquires cleanly.

### 3.7e Payload reverse-engineering discipline: verify before trusting a description, then verify the trust itself

Asked to extract and store a new data field (room square footage) based on a
stakeholder's description of where it lived in a third-party API response.
The described path did not exist in the real payload — a live probe was run
before writing any extraction code, and the actual location was found one
level over. That habit paid for itself immediately: the field's *values*
were then re-validated for a second failure mode — a hotel-level constant
masquerading as per-room data — by checking a hotel where every room
happened to report the same size, which would have looked identical to a
genuine bug. Cross-checking a second, structurally different hotel showed the
value **correctly varying with room tier** (30 m² → 80 m² across a 6-room
aparthotel, tracking bedroom count), which is what separated a real signal
from a plausible-looking mistake before either was shipped.

The schema change this required (`ALTER TABLE`, the one operation this
project's own additive-only design forbids everywhere else) was treated as a
standing-rule exception requiring explicit authorisation, not something to
silently work around — proposed with real observed values, two design
options costed against each other (a single denormalised text field vs. a
queryable numeric column), and implemented only after an explicit decision on
which. The write path itself stayed inside the additive-only contract: one
narrowly-scoped, separately-documented `UPDATE` that can *only* fill a
currently-`NULL` value and is structurally incapable of overwriting one that
is already set (`COALESCE` in the assignment, not a runtime check) —
verified with a live lifecycle test against the production database: insert
empty → backfill → a second, different value is refused → a direct call to
the guarded update itself against a filled row changes zero rows.

### 3.7f Operability

- **A wizard-driven interactive mode** resolves an operator's typed-in
  city name or id against the live DB, cross-references a persistent ledger to
  show *runnable-now* vs. *already-published-and-skipped* hotel counts before
  a single API call is made, and supports stepping backward through any
  earlier decision. Automation (cron) uses the same code path via flags,
  skipping the wizard entirely.
- **Three distinct human-facing report surfaces**, each answering a different
  question: `hotels_to_revisit.csv` (what failed and why, so it can be
  re-attempted), `rooms_review.csv` (a candidate a human should eyeball —
  includes the actual image URL to look at, not just a score), and
  `rooms_unmatched.csv` (genuinely nothing to review). All flat CSV,
  deliberately, over a nested JSON — a spreadsheet-filterable report is what a
  non-engineer reviewer actually needs.
- `--dry-run` exercises the entire pipeline — matching, image download,
  categorisation — without writing to the database, so a new city can be
  sanity-checked before it touches production data.

---

## 4. Quantifiable, CV-ready facts

Use these as direct bullet points; each is a real, measured number from this
project, not a rounded-up estimate.

- Built and shipped a **7-module production data pipeline** (~2,500 lines)
  integrating **2 undocumented third-party HTTP APIs, 1 production MySQL
  database, 1 S3-compatible object store, and a headless-browser fallback**
  for JS-gated responses.
- Designed a **fuzzy cross-platform entity-resolution engine** combining
  string similarity, geospatial verification (haversine distance), and
  domain-specific hard-veto rules — tuned against real production data to a
  measured precision/recall split (72–100 candidate floor, 88 precision gate,
  75/62 accept/review room-match thresholds).
- **Unicode-safe, multi-script name normalisation** — fixed a correctness bug
  that silently erased every non-Latin-script name to empty, which would have
  made the system unusable outside Latin-alphabet markets.
- Engineered a **25-category regex-based classification taxonomy** with
  explicit precedence rules, validated against 32 hand-picked edge cases
  spanning ambiguous real-world room names.
- **Cost-engineered hotel identity from O(hotels) to O(1) per city** by
  recognising a data source (the operator's own database) made an entire
  network-search phase redundant for *finding out who exists*. Later
  eliminated the one remaining network-bound phase too (per-hotel room
  names) by discovering and exploiting a per-hotel-scoped partner endpoint —
  replacing a whole city-wide polling search with a direct lookup keyed by
  the database's own slug, cutting a measured cost-per-hotel by roughly 12×
  and a targeted few-hotel re-run by roughly 66× (~6.5 min → ~6 s), verified
  with a live before/after A/B, not estimated.
- Measured and codified **two counter-intuitive operational parameters**
  through live A/B-style testing against production endpoints (probe-date
  offset, occupancy=2 vs. 1), each with the supporting numbers recorded
  alongside the code, not lost to institutional memory.
- Designed a **crash-safe, resumable pipeline** with hotel-level transactional
  atomicity and content-addressed idempotent object storage — proven
  correct via reproducing an actual mid-run failure (dropped DB connection),
  not just via unit tests.
- **Found and fixed a cascading-failure bug in my own reliability code**
  before it shipped: a single-attempt database reconnect that, on failing
  itself (the normal case, since the retry lands inside the same outage),
  silently left every subsequent hotel in a multi-hour run failing against a
  closed connection. Rebuilt as exponential backoff (up to 6 attempts, ~2
  minutes) applied to every database read the pipeline performs, not just
  the write path, and proved the returned connection object is never the
  broken one — not just that the call returns without an exception.
- **Implemented and live-verified graceful shutdown** for the signal a real
  orchestrator actually sends (`SIGTERM`), previously unhandled — sent a real
  signal to a real running process, confirmed it finished its in-flight
  transaction and checkpointed cleanly, then confirmed a second process
  resumed from exactly that checkpoint rather than restarting from zero.
- **Added crash-recovery checkpointing** to the longest unattended stage of
  the pipeline, and caught a real scope-leak bug in my own first version
  before it shipped (a checkpoint could be silently reused across two
  different cities' runs and cause one to report hotels as checked when they
  were never actually asked about) — fixed with a fingerprint over both the
  exact hotel set and the search parameters, covered by a dedicated test
  proving no cross-scope leakage.
- **Made the local durability guarantee provable, not assumed**: added
  `fsync` to every ledger write and verified it from a genuinely separate
  process reading the file immediately after the write call returned, rather
  than trusting the writer's own belief that the bytes were safe.
- **Ran a dedicated wasted-work audit** (a distinct discipline from
  correctness or performance review — "does this system already have an
  answer and pay to re-acquire it") and found a real instance hiding behind
  passing tests: a room's images were downloaded and re-uploaded *before*
  checking whether the database already had one, not after. Reordering the
  check against the same query the write path already ran cut a measured
  re-run from **577 redundant image transfers to 46**, with the published
  result byte-for-byte identical — and explicitly declined to "fix" a
  second, superficially similar case (an escalation ladder that runs
  unconditionally) after confirming it was a deliberate correctness decision
  from a separate diagnosis, not an oversight.
- Implemented and **live-verified a runtime security guard** preventing a
  credential with full database privileges from ever executing a destructive
  statement, closing the gap between "the grant restricts it" and "the code
  guarantees it."
- Ran a **live domain-truth audit** on a production data pipeline and found +
  fixed a real correctness bug (an unearned "zero" result from an escalation
  loop exiting early) — the kind of defect that produces no error, no
  exception, and a fully schema-valid, silently wrong answer.
- Built a **cross-run ledger system** (append-only, O(1) writes, last-write-
  wins semantics) tracking both successful publishes (for staleness-based
  skip logic) and failures (for targeted re-attempts), without a database
  schema I was permitted to extend.
- Delivered an **operator-facing interactive CLI** with stateful back/forward
  navigation and live cost transparency (runnable vs. skipped hotel counts
  against a configurable staleness window), alongside a fully
  flag-driven non-interactive mode for automation.
- **Validated the entity-resolution engine against 10 countries and 4
  scripts** in a structured campaign (not incidental usage), specifically
  designed to surface market-specific bugs a single-city build would hide —
  found and root-caused 4 distinct defect classes this way, none of them
  visible in the original build/test market.
- **Measured a silent-failure blast radius before fixing it**: a single
  hardcoded regional assumption in an address-parsing fallback left
  **~4,400 hotels (~5% of an 89,000-hotel catalogue)** with a non-functional
  fallback match path — quantified from 4,000 sampled real records, not
  estimated.
- **Found and closed a live, unprompted false-positive** in a destination
  name-resolution dependency: two real inputs (a German-language city name; a
  Peruvian province) independently resolved to the *wrong country* on a
  third-party API (Switzerland; Mexico) — the kind of defect a single-market
  test suite structurally cannot surface — closed with a cross-source
  agreement check that flags every future disagreement rather than trusting
  either source blindly.
- **Ran a self-audit specifically for overfitting** (not general QA) and
  found genuine overfitting in **5 of 5** examined fixes, including one test
  that had been written to match a bug's output rather than the correct
  behaviour — then **mutation-tested the replacement test suite** to prove
  the fixes had real coverage rather than assuming: reintroducing a known-bad
  implementation made **46 of 60** property-based test runs correctly fail.
- **Recalibrated a fuzzy-matching system for an explicit business priority**
  (recall over precision, because a false negative is costlier than a
  near-miss in this domain) and proved the change cost **zero** recall while
  raising same-hotel discrimination from **30.3% to 55.8%** — measured in
  both directions, not optimised against one metric alone.
- **Found and closed the highest-severity risk in the system** during a
  self-directed production-readiness audit: a missing database unique
  constraint meant concurrent runs could permanently duplicate data with no
  recovery path (the system is contractually forbidden from issuing
  `DELETE`) — closed with an OS-level advisory lock, proven correct with a
  genuine two-process concurrency test, not reasoned about in the abstract.
- **Reverse-engineered an undocumented third-party payload structure from
  first principles** rather than trusting a stakeholder's description of it,
  catching a described-but-nonexistent field path before writing any
  extraction code, and independently verified the field's authenticity (real
  per-room variance vs. a masquerading constant) across multiple properties
  before it reached production.
- **Governed a schema change as a standing-rule exception**, not a routine
  edit: proposed with real sampled values, presented as a costed choice
  between two storage designs, implemented only after explicit authorisation,
  and the resulting write path kept inside the project's own additive-only
  guarantee via a single column-scoped, `COALESCE`-guarded `UPDATE` proven
  incapable of overwriting a set value.
- Measured that a hand-rolled classification/normalisation layer (25-category
  taxonomy + name matching) costs **6 microseconds per room — 0.065 seconds
  for an entire 1,340-hotel city** — establishing with a number, not an
  assumption, that compute-side logic was never the bottleneck worth
  optimising, only network-bound work was.

---

## 5. Tech stack

`Python` · `MySQL 8.0 (txsql)` · `pymysql` · `Tencent COS (S3-compatible, via boto3)`
· `Playwright` (headless Chromium, network-layer response interception, not DOM
scraping) · `rapidfuzz` · `requests` · `concurrent.futures.ThreadPoolExecutor`
(bounded I/O-bound concurrency) · `fcntl` (OS-level advisory file locking for
cross-process mutual exclusion) · reverse-engineered and partner-documented
REST APIs (Bookme partner `/availability`, Agoda suggest/room-grid) · CSV-based
operational reporting · `.env`-based credential management · `ruff` for static
analysis · assert-based self-testing (no test framework dependency) ·
property-based testing (order-invariance via randomised shuffling) ·
mutation testing (deliberately reintroducing known-bad logic to prove test
coverage, not assume it)
