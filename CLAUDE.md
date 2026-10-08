# Olive AI — Project Instructions for Claude

Olive AI is a Hudson Intelligence product: an AI-powered Nasdaq-100 futures
(NQ/MNQ) market-intelligence and trade-signal platform, developed in 14
sequential phases (see `docs/architecture.md`). Phase 1 (foundation) and
Phase 2 (futures domain, hardened through corrective passes 2.1–2.4) are
complete. Current status and capabilities: see `README.md`.

Standing rules that apply regardless of which phase is in progress:

- Phases are sequential. Do not implement scope from a later phase while
  working on an earlier one, even if it would be convenient (no market
  data, providers, prices, bars, strategies, ML, signals, backtesting,
  paper trading, web UI, or database work until the phase that is actually
  scoped for it).
- Keep system/data-state labels truthful at all times: `LIVE`, `DELAYED`,
  `HISTORICAL`, `SIMULATED`, `DEMO`, `NOT_CONFIGURED`, `STALE`,
  `CONFIGURED`, `VALID`, `HEALTHY`, etc. must never be reported unless the
  underlying evidence actually supports that state. Fail closed rather
  than fabricate a fallback value to preserve a green status.
- Never commit secrets. `.env` and other local secret files stay
  git-ignored.
- Do not commit or push unless the user explicitly authorizes it in that
  turn. Always inspect `git status` truthfully and report it.
- Do not weaken, delete, or water down an existing test merely to make it
  pass. Fix the underlying code instead, or raise the discrepancy.
- GitHub (the user's own machine/repo) is the source of truth; this cloud
  workspace delivers corrective work as a ZIP, not via commits made here.

## Standing QA / self-review instruction (applies to every phase and every corrective revision)

This was established as a permanent policy, not a one-off request — apply
it in full every time, without being asked again.

**Before declaring any implementation complete**, go beyond the explicit
happy-path requirements and beyond the tests already written. Perform an
adversarial engineering review of your own work, as if another senior
engineer were deliberately trying to break it before it reaches
production.

For every public API, domain object, loader, service, and boundary
created or modified, deliberately test (where applicable):

- malformed input types; `None`; booleans where integers are expected;
  empty and whitespace strings; invalid enum values; zero and negative
  values where inappropriate; extreme values; `NaN`/`Infinity` wherever
  numeric input exists; incorrect timezone/date/datetime types; boundary
  timestamps; mismatched objects from different instruments/providers/
  models; duplicate identifiers; inconsistent metadata; direct object
  construction that bypasses loaders; corrupted, partial, or
  unexpected-extra configuration; missing files; stale state; failure
  paths; year/month boundaries; session boundaries; rollover boundaries;
  serialization/deserialization round trips where relevant.

Do not assume Python type hints enforce runtime invariants. Do not assume
a loader's validation makes the underlying domain object safe when it is
constructed directly instead. Expected malformed caller input must fail
with an appropriate Olive/domain-specific error (a subclass of
`FuturesDomainError`, or the equivalent for whatever subsystem is being
built) — never a leaked raw `AttributeError`, `KeyError`, `TypeError`,
`ValueError`, `decimal.InvalidOperation`, `OverflowError`, or similar. Do
not achieve this by broadly swallowing exceptions.

Before considering anything complete, ask:

1. Can this object be constructed in an impossible state?
2. Can two valid objects be combined in an invalid way?
3. Can a caller bypass validation through another code path?
4. Can malformed numeric values enter the system?
5. Can a type hint be violated at runtime?
6. Can boundary dates/times create an off-by-one error?
7. Can corrupted configuration still produce a healthy system state?
8. Can this component falsely report LIVE / CONFIGURED / VALID?
9. Can an unexpected dependency failure produce misleading output?
10. Can a structurally valid but factually wrong configuration still pass?
11. Can a future update accidentally weaken a previously validated
    invariant?
12. What inputs would an external reviewer use specifically to break this
    implementation?

Add meaningful regression tests for anything discovered this way. Do not
inflate the test count with trivial assertions — correctness and
invariant protection matter more than the number of tests.

### Generic structural validity vs. Olive production correctness

Always keep these two questions separate, and know which one a given
check answers (this distinction is precedented in the codebase already —
see `docs/futures_domain.md` §9 and `app/futures/validation.py`, added in
Phase 2.4 for exactly this reason):

- *Generic structural validity*: "Is this object/config internally
  self-consistent?" (e.g. `tick_value == tick_size * multiplier`). A
  generic domain object, calendar, provider response, or loaded model can
  satisfy this while still being wrong for Olive's actual purposes.
- *Olive production correctness*: "Is this specifically the real,
  intended NQ/MNQ product / the required official calendar coverage / the
  freshly-labeled correctly-sourced data / the correct validated model
  version Olive actually requires in production?" Never let "internally
  consistent" be mistaken for "correct for Olive production" — keep the
  generic domain objects/loaders generic, and put the Olive-specific
  requirement in its own, separate, reusable validation layer (as
  `app/futures/validation.py` does for the futures domain).

### Financial/market domain QA (for any future market-data, signal, prediction, or backtesting work)

When that work begins, deliberately test: stale timestamps; duplicate
ticks/bars; out-of-order events; missing events; invalid prices; negative
volume; incorrect contracts; contract-rollover boundaries; closed-market
timestamps; holiday/special-session assumptions; timezone/DST boundaries;
provider partial failure; delayed data mislabeled as live; simulated data
mislabeled as live; future leakage; look-ahead bias; inconsistent model
versions; invalid probability distributions; predictions older than
their horizon; stale signals; event-risk windows; conflicting strategies;
missing features; model-drift states; impossible P&L/fill assumptions.

### Vendor/provider integration lessons (from Phase 3.1, apply to every future provider integration)

Independent review of the delivered Phase 3 Databento adapter found
defect classes general enough to check for in any future provider
integration (a second historical-data vendor, a real-time feed, a
broker API, ...):

- **Never let a vendor exception's own message text reach anything
  this codebase raises, logs, or returns.** A vendor's documented
  error shape can itself embed a credential (Databento's own
  invalid-Basic-auth error literally contains the API key in its
  message). Classify the vendor exception's status/message internally
  to pick a category, but raise only a fixed, pre-written, generic
  message for that category -- never an f-string built from
  `str(vendor_exception)`. Sever the exception chain (`from None`) so
  even a full traceback print cannot surface the original.
- **A vendor's own symbol/ticker is not necessarily a stable,
  unambiguous identity.** A raw symbol that encodes only part of a
  date (Databento's raw futures symbols encode only the last digit of
  the contract year, so `NQZ6` is reused across 2016/2026/2036) must
  be resolved to a stable identity (e.g. an instrument ID, confirmed
  stable over the exact window being requested) via a free/cheap
  lookup *before* any paid or consequential call -- a response-level
  "does the vendor's symbol field match what I asked for" check is
  necessary but not sufficient, because a wrong-identity response can
  still honestly self-report the symbol the caller asked for.
- **Never let a provider-returned value silently participate in a
  numeric conversion that can lose information.** Prefer
  `operator.index()` over `int(...)` for an expected-integer vendor
  value -- `int()` silently truncates a float (`int(1.5) == 1`,
  undetectable corruption), while `operator.index()` rejects every
  float (bool must still be excluded explicitly first, since
  `bool.__index__` exists).
- **Validate a provider's return TYPE before using it, even though the
  provider implements an ABC/interface.** An ABC only enforces that
  the right methods exist, not that a concrete (possibly broken or
  malicious) implementation returns the right type. A falsy
  non-sequence (`None`, `{}`, `""`) must never be treated as the
  documented "empty success" representation unless it is actually of
  the documented empty-sequence type.
- **A storage pair that must stay consistent (data file + manifest/
  metadata file) is not atomic just because each half is written
  atomically.** A failure between the two separate atomic writes can
  still leave a mismatched pair. Capture the previous valid state
  before writing either half, and roll back on a failure in the
  second half.
- **A manifest/checksum that is written but never verified on read is
  not a real integrity guarantee** -- it only documents an assumption.
  Verify it every time data is read back, not only at write time.

### Result-object and storage-transaction lessons (from Phase 3.2, apply to every future "structured outcome" object and transactional storage layer)

A second independent review of the actual delivered Phase 3.1 ZIP
found further defect classes, general enough to check for whenever a
new structured-result dataclass or transactional storage path is
built:

- **A "structured result" domain object (the whole point of which is
  to be a trustworthy stand-in for exceptions/`None`/ambiguous
  values) must validate itself exactly like any other domain object.**
  `HistoricalWriteResult`/`HistoricalReadResult` initially had no
  `__post_init__` at all, and independent review constructed negative
  counts and an internally-inconsistent `new_records > total_records`
  successfully. Any field that should never be negative, and any
  cross-field relationship implied by the object's own meaning (a
  count of "new" items can never exceed a count of "total" items),
  must be enforced in `__post_init__`, not left to the caller's good
  behavior.
- **An existing stored/cached value being read back for a MERGE or
  UPDATE is just as untrusted as a value being read back for a plain
  read.** `write_bars` read an existing partition directly for merge
  purposes, bypassing the same manifest/checksum verification the
  public `read_bars` path already enforced -- letting a later write
  "launder" previously unverified or corrupt data into a brand-new
  trusted manifest. Any code path that reads persisted data to merge
  or update it needs the identical integrity gate as a path that reads
  it to return it.
- **A rollback path's OWN failure must never be silently swallowed
  with a bare `except Exception: pass`.** If the thing being rolled
  back to recover from a failure ALSO fails, that is new, more urgent
  information (storage may now be inconsistent and need operator
  intervention) -- not a reason to let the caller believe the
  original failure was handled cleanly. Collect the rollback failure
  and raise a dedicated error chaining both failures.
- **A public domain method on an otherwise-hardened object can still
  leak a raw exception if IT was never covered by the standing
  public-boundary discipline.** `HistoricalBar.__post_init__` was
  thoroughly validated, but its own `conflicts_with(other)` method
  still assumed `other` was a well-formed `HistoricalBar` and leaked a
  raw `AttributeError` otherwise. Every public method that touches
  attributes of a parameter needs the same argument-type check as a
  constructor, not just the constructor itself.
- **A dependency-injection seam is a public boundary too.** An
  injected test double is a deliberate, valuable design choice (never
  require the real heavyweight client to run tests) -- but the
  CONSTRUCTOR accepting that injected value must still validate the
  interface it actually needs (the specific callables it will call),
  the same way any other public constructor validates its arguments.
  Accepting anything and failing later with a raw `AttributeError` the
  first time a missing method is touched is the exact defect class the
  rest of this codebase's public-boundary discipline exists to
  prevent.
- **A "never depends on the current working directory" guarantee
  claimed in a module's docstring must hold for EVERY construction
  path, including direct construction with a relative path.** Storing
  a relative `Path` and re-resolving it against the process's CWD on
  every later call means the exact same object can silently point at
  two different physical locations before and after an unrelated
  `os.chdir()` elsewhere in the process. Resolve to an absolute path
  once, at construction time, if the guarantee is to mean anything.
- **A per-row defense-in-depth identity check must never degrade into
  fabricating the very provenance it exists to verify.** A response
  row missing an expected identity field (here, Databento's
  `instrument_id`) must fail closed -- substituting Olive's own
  "expected" value as a fallback silently defeats the entire purpose
  of checking the field in the first place, even though the resulting
  object still looks well-formed.
- **A half-open date/datetime boundary fix in one place does not
  automatically carry over to a DIFFERENT representation of a related
  boundary elsewhere.** The storage layer's own half-open
  month-boundary fix (`_months_between`) did not stop an analogous
  half-open DATE-vs-DATETIME boundary bug from existing in the
  Databento adapter's symbology date-range projection -- each
  half-open interval conversion must be checked on its own terms, even
  when a sibling conversion elsewhere was already fixed correctly.
- **"Internally self-consistent" is still not "correct for Olive
  production," even for a value a provider returns alongside
  otherwise-good data.** A returned bar's `tick_size`/`provider`/
  `provider_raw_symbol` can each individually look fine on a
  self-consistency check while still being wrong for the specific
  production instrument/provider Olive is actually dealing with in
  that request -- cross-check provider output against the
  already-production-validated domain object, not just against the
  bar's own internal arithmetic.

### Canonical-storage and defense-in-depth lessons (from Phase 3.3, apply to every future deduplication policy, partition/shard identity check, and manifest-content verification)

A third independent review of the actual delivered Phase 3.2 ZIP found
further defect classes, general enough to check for whenever a future
subsystem tolerates duplicates, partitions data by a derived key, or
writes a descriptive manifest/index alongside the data it describes:

- **Canonicalizing (deduplicating) tolerated-duplicate state must
  happen BEFORE, not after, computing any count that feeds a
  structured result object's construction.** `write_bars` computed
  `existing_count` from an existing partition's RAW row count; a
  partition already (legitimately) holding two identical copies of
  one bar made `len(merged) - existing_count` go negative the moment
  that same bar was submitted again -- failing `HistoricalWriteResult`
  construction AFTER the write had already durably committed. The fix
  is structural, not defensive: canonicalize first, so the quantity
  being checked can no longer go negative by construction, rather than
  adding a clamp or a special case after the fact.
- **A manifest's own self-declared identity fields are not independent
  evidence if nothing checks the data itself too.** A manifest can be
  forged (or corrupted) to correctly claim the right directory/
  partition/shard while the actual content inside is still misplaced —
  manifest-vs-directory agreement and content-vs-directory agreement
  are two SEPARATE checks, and a defense-in-depth design that only
  implements the first is not actually defense in depth. Verified here
  by deliberately constructing that exact adversarial case (forged
  manifest, genuinely misplaced content) and confirming the per-content
  check still catches it alone.
- **A grouping/partition key only guarantees agreement on the fields
  it actually contains.** `write_bars`'s own grouping key covered
  `provider`/`dataset`/`root_symbol`/`contract_identity`/`timeframe`/
  year/month, but NOT `tick_size`/`provider_raw_symbol`/
  `provider_instrument_id`/`data_label`/`schema_version` -- so two
  individually well-formed bars with different economics/provenance
  could land in, and later be read back from, the same partition
  without anything ever comparing those specific fields. A derived
  grouping key is not a substitute for an explicit coherence check on
  every field the grouping key does not cover.
- **A manifest field that is written with a rich value but never
  cross-checked against the content it describes is not a real
  guarantee for that field**, even when the manifest's raw-bytes
  checksum IS verified -- a checksum only proves the bytes have not
  changed since they were written, not that the manifest's own
  descriptive claims about those bytes were ever correct relative to
  what the bytes actually decode to.
- **When a tolerance policy (identical duplicates are safe to
  collapse) and a verification policy (manifest content must match
  decoded content) interact, get the ORDER right deliberately, not by
  accident.** Initially canonicalizing before verifying manifest
  content made a legitimate, already-tolerated duplicate look like a
  manifest/content mismatch (`record_count=2` vs. 1 unique bar after
  dedup). The fix was to define what each check is actually supposed
  to mean ("manifest content" means raw rows on disk, not unique
  canonical keys) and run every content-level check against RAW
  decoded data, with canonicalization strictly LAST, immediately
  before the data is used -- not to loosen either check.
- **A validated enum/string FORMAT at a provider boundary is not the
  same as a validated enum/string VALUE.** Databento's resolved
  instrument ID is documented as an integer but transported as a
  string; validating "is this a string" is necessary but not
  sufficient -- it must also be non-empty, composed only of true ASCII
  digits (Python's `str.isdigit()` returns `True` for non-ASCII digit
  characters too, a trap worth checking explicitly for any
  provider-supplied numeric-looking string), and strictly positive.
  Validate entry SHAPE (is this even a usable mapping with the right
  key) and value FORMAT (is the extracted value well-formed) as two
  separate, explicitly-ordered checks, not one combined assumption.
- **A numeric domain invariant should encode WHY the bound exists, not
  just that a bound exists.** `HistoricalBar`'s new tick-count/volume
  ceiling is `ARROW_INT64_MAX = 2**63 - 1` specifically because that is
  the exact signed-64-bit range the storage schema declares for those
  columns -- a storage-representability bound, not an arbitrary
  "sensible trading volume" guess. Naming the constant after the real
  constraint (and pointing it at the schema that imposes it) keeps a
  future change to the storage schema from silently making the two
  disagree again.

### Nested-boundary adversarial testing and completion-claim lessons (from the Phase 3.3 QA compliance rework, apply to every future provider/storage boundary and every future completion report)

A fourth independent review of the actual delivered Phase 3.3 ZIP found
that the baseline itself (1068 passed, 2 skipped, healthy `main.py`,
correct layout, no Phase 4 scope) was genuinely strong, but the
completion PROCESS had still declared every requirement satisfied
without the underlying verification having actually been performed
exhaustively -- three confirmed public-boundary defects and one
confirmed impossible-state defect survived a prior "all green" claim.
Two durable lessons, general enough to apply well beyond this one
correction:

- **Testing only LEAF values never proves a nested container is safe --
  every level of a structured response or stored document must be
  mutated independently.** Testing `resolution["result"]["NQZ6"][0]["s"]
  = None` does not prove `resolution["result"]["NQZ6"] = 123` is safe,
  and testing `manifest["partition_year"] = "bad"` does not prove
  `manifest["schema_version"] = True` is safe -- each is a DIFFERENT
  boundary at a different nesting level, and Python's own equality/
  coercion semantics (`True == 1`, `True in {1}`, `1.0 == 1`, and the
  `value or []` pattern only replacing FALSY values, never type-
  checking) are part of the attack surface, not just outright wrong
  types. A complete adversarial pass for any provider response or any
  persisted structured document mutates: the whole top-level object,
  every top-level field, every nested collection, every individual
  entry within a collection, AND every individual leaf value -- never
  stopping once leaf-value testing is complete. For manifest/schema
  validation specifically, this means a single reusable schema-
  validation boundary (type-checking `isinstance(value, bool)` BEFORE
  `isinstance(value, int)`, for every integer-typed field, before any
  `!=`/`in` comparison runs at all) rather than relying on scattered
  equality checks that happen to work for well-formed input.
- **"All requirements satisfied" is a claim that must be backed by an
  evidence ledger, never declared from the code "looking right."**
  Forbidden reasons to mark a requirement PASS: the code looks right, a
  nearby test happens to pass, a type hint implies the right type, or
  the happy path works. From this correction forward, a completion
  report never states "all requirements addressed" / "all green" / "no
  known gaps" unless a compliance ledger (every prompt requirement and
  every applicable standing rule, each with its implementation
  location, regression test, manual adversarial check, and an explicit
  PASS/FAIL/NOT APPLICABLE status with real evidence) has been built
  FIRST and contains no unverified item. Anything inferred rather than
  directly tested is labeled "INFERRED / NOT DIRECTLY VERIFIED," never
  PASS. Every completion report includes a compact compliance summary:
  prompt requirements verified (X/X), standing QA boundaries
  adversarially tested (Y/Y), skipped/unverified requirements listed
  explicitly (or "NONE"), manual adversarial findings, and known
  limitations.

### Member-level and sibling-entry validation lessons (from the Phase 3 final completion pass, apply to every future nested/structured provider response or persisted document)

A fifth independent review of the actual delivered Phase 3.3 QA-rework
ZIP found that the "every level of nesting" discipline from the
previous rework had itself stopped one level too early in two places —
a CONTAINER's type being validated was mistaken for proof that its
MEMBERS were valid, and a REQUESTED key's slice being validated was
mistaken for proof that its SIBLINGS in the same response were valid.
Two further durable lessons, on top of (not replacing) the Phase 3.3 QA
rework's "every level of nesting" rule:

- **Validating that a collection IS the right container type is not
  the same as validating what is INSIDE it.** `not_found`/`partial`
  were checked with `isinstance(value, (list, tuple))` and treated as
  safe — but `not_found=[123]` passes that check, then silently fails
  the `raw_symbol in not_found` membership test the same way a correct
  "not flagged" response would, because a string never equals an int.
  A malformed member inside an otherwise well-typed container is not
  a type error that crashes loudly; it is a silent, wrong, FALSE
  answer that looks like a legitimate negative. Every container-typed
  field at a provider or storage boundary needs its own per-member
  shape check, run before any membership test, equality comparison,
  or indexing touches the untrusted contents.
- **A requested key's own entry being well-formed proves nothing about
  its siblings in the same structured response.** `result[raw_symbol]`
  was validated in full, but `result["some_other_symbol"]` could be
  malformed without ever affecting whether the requested symbol's own
  lookup succeeded — accepting a response that is only PARTIALLY
  trustworthy, as long as the one slice the caller happened to read
  was the trustworthy part. If a structured response is supposed to be
  internally trustworthy at all (rather than merely "the one key I
  asked for turned out fine"), every key, every value, and every leaf
  inside the whole mapping must be validated before any single key is
  read out of it — not just the slice the immediate caller needs.
- **A value nothing reads cannot affect correctness — but only once
  you can show the properties it would have protected are already
  guaranteed some other way.** Databento's `d0`/`d1` symbology fields
  are never consumed by any Olive code path, so no malformation of
  them can produce a wrong result; this was a legitimate reason to
  skip adding validation for them, but only because the two safety
  properties `d0`/`d1` would otherwise exist to protect (no identity
  ambiguity within the requested window; full coverage of that window)
  were independently traced to other, already-enforced checks (the
  distinct-instrument-ID count check; Databento's own `not_found`/
  `partial` classification). "Nothing reads this field" justifies
  skipping validation only after that independent-guarantee tracing is
  done explicitly — never as a default assumption, and never as a
  reason to skip checking a field whose protected properties are NOT
  otherwise guaranteed.

### Streaming/live-connection and counter-correctness lessons (from Phase 4, apply to every future streaming provider integration and every future "structured status/telemetry" object)

Phase 4 (real-time market data) surfaced defect classes distinct from
Phase 3's one-shot-fetch lessons above, specific to a long-lived
streaming connection with its own reconnect/backoff lifecycle and its
own running telemetry counters. General enough to check for in any
future streaming integration (a second real-time vendor, a broker
order-update stream, a websocket feed, ...):

- **A telemetry counter must be incremented at every site the thing it
  counts actually happens, not just the one internal helper that
  happens to run most of the time.** `DatabentoLiveProvider`'s
  `reconnect_count` was incremented correctly for every retry *inside*
  the shared `_connect_with_reconnect` helper -- but a transient
  failure encountered mid-stream (inside the long-lived `events()`
  generator, calling that same helper to recover) did not separately
  count as a new reconnection event once it succeeded. Before trusting
  a counter in a status object, enumerate every call site that can
  legitimately cause the thing being counted, not just the most
  obvious one, and add a regression test that exercises the *less*
  obvious site specifically (here: a transient failure after a
  stream is already open and flowing, not only one at initial
  connect time).
- **A fetched-but-unused value is a sign of missing enforcement, not
  acceptable dead code.** The adapter looked up each subscribed
  instrument's own `tick_size` in `_normalize_record` and then never
  used it -- the variable existed because tick-alignment enforcement
  was the obviously-intended reason to fetch it, but the enforcement
  itself had been left out. Before leaving an unused local variable in
  place (or deleting it to "clean up"), ask what invariant its
  presence implies should be checked, especially for a value pulled
  from an already-validated domain object specifically to cross-check
  untrusted provider data against it.
- **Per-record field validation inside a long-lived stream must be
  caught at the SAME narrow scope as the rest of that record's
  validation, not left to escape to the whole stream's own exception
  handling.** Adding real tick-alignment enforcement initially placed
  the new check *outside* the existing `try/except LiveDataError`
  block that already wrapped a trade/quote/bar's other field
  conversions in `_normalize_record` -- so a single tick-misaligned
  price would have propagated uncaught and killed the entire stream,
  instead of being rejected as just that one malformed record (the
  behavior every *other* field-validation failure in the same method
  already had). When adding a new validation step to an existing
  per-item error-handling block, always re-check that the new call is
  actually inside that block, not appended after it by mistake.
- **A vendor's wire-level sentinel convention that cannot be verified
  against the real installed package must be resolved by the safest
  available alternative, documented explicitly as provisional, and
  flagged for verification -- never silently guessed at as if it were
  confirmed.** Databento's exact "no resting order on this side of the
  book" convention for a live quote could not be checked against the
  real `databento` package in this sandbox; rather than assume an
  unverified price-sentinel value, the adapter gates side-presence on
  the order's SIZE (unambiguous: zero size always means no order,
  regardless of what the paired price field contains). The decision
  itself was sound, but the discipline that matters here is general:
  when a vendor integration cannot be verified end-to-end, prefer the
  interpretation that degrades safely if wrong, write down which
  specific assumption was substituted and why, and say so again in the
  completion report's known-limitations section -- never let an
  unverified convenience assumption read as settled fact.

### Re-verified vendor API shape and persistent-subscription identity lessons (from the Phase 4 correction pass, apply to every future streaming-provider integration and every future correction of a previously delivered build)

An independent audit of the actual delivered Phase 4 build (not a new
phase -- a single consolidated correction to the same Phase 4
delivery) found that several of the "provisional, flagged for
verification" assumptions documented above turned out to be
outright wrong against the vendor's own documented API, not merely
unverified, and that a vendor-side ambiguity problem Phase 3 had
already solved for a one-shot fetch resurfaced, unaddressed, in a
genuinely different form for a persistent subscription. Durable
lessons, on top of (not replacing) the Phase 4 lessons above:

- **"Documented API shape, not independently verified against the
  real package" is not the same risk tier as "plausible-looking API
  shape, never checked against the vendor's documentation at all."**
  The originally delivered adapter constructed
  `databento.Live(key=..., dataset="GLBX.MDP3")` and called
  `client.start()` before synchronously iterating -- neither matches
  the documented 0.87 API (`Live` takes no `dataset`; `start()` before
  synchronous iteration is documented to raise `ValueError`), and
  neither was flagged as an assumption anywhere, because neither was
  ever checked against the vendor's documentation in the first place.
  Every external call this codebase makes into a third-party client
  needs its exact documented signature and lifecycle actually
  consulted once, not inferred from a similar-looking call elsewhere
  or from what "seems like" it should work -- and the compatibility
  test doubles exercising that call need to be strict enough to catch
  a regression back to the wrong shape (a fake constructor accepting
  `**kwargs` unconditionally cannot catch an erroneously-passed
  parameter; a fake requiring the exact documented parameter list
  can).
- **A vendor ambiguity problem solved for a one-shot fetch is NOT
  automatically solved for a persistent subscription, even by the
  exact same underlying vendor capability.** Phase 3's historical
  adapter already resolved Databento's raw-symbol decade-reuse
  ambiguity (`NQZ6` means a different contract in 2026 vs. 2036) via a
  point-in-time `symbology.resolve` lookup before every paid fetch --
  but the originally delivered Phase 4 live adapter never did the
  equivalent for a live subscription, instead matching an incoming
  `SymbolMappingMsg` to "the first subscribed contract sharing that
  raw symbol." The underlying vendor ambiguity was identical; the
  SHAPE of the fix had to be re-derived for the new context (a
  pre-connection identity proof scoped to each contract's own
  calendar month, performed once per `connect()`, via a SEPARATE
  metadata client) rather than assumed solved because a sibling
  module already solved "a" version of it. When a correction
  introduces a second consumer of an already-hardened pure function
  (here, `_distinct_resolved_instrument_id`, reused rather than
  reimplemented), reusing the function is right, but reusing it does
  not by itself prove the NEW call site's surrounding architecture
  (when it's called, relative to what, and what happens on failure)
  is correct for the new context -- that still has to be designed and
  tested on its own terms.
- **A telemetry counter's documented meaning must be enforced at
  EVERY site that mutates it, including a site that increments it for
  the WRONG reason.** The Phase 4 lessons above already cover a
  counter that failed to increment at a legitimate site (a mid-stream
  recovery). This correction found the complementary defect: the SAME
  counter (`reconnect_count`, documented as "successful reconnections
  only") was ALSO incremented at a site that should never have
  touched it at all -- a failed attempt during the INITIAL connection,
  which is not a reconnection (nothing was previously connected to
  lose). A counter review must check not only "does it increment
  everywhere it should" but also "does it increment ONLY where it
  should" -- the two are independent failure modes, and fixing one
  does not imply the other was ever checked. The fix here was
  structural: split the single counter into two truthfully-named
  ones (`reconnect_count` for successful re-establishments only,
  `reconnect_attempts_total` for every attempt anywhere), rather than
  trying to make one field honestly mean two different things.
- **A non-session-fatal vendor condition that nonetheless represents
  real data loss must be surfaced as its own explicit state, never
  folded into the generic "harmless, streaming continues" bucket
  merely because it isn't fatal.** `SKIPPED_RECORDS_AFTER_SLOW_READING`
  is documented as not closing the connection -- but silently treating
  it exactly like `SYMBOL_RESOLUTION_FAILED` or any other harmless
  non-fatal code would let Olive report a fully healthy stream while
  actually missing data. "Not fatal" and "harmless" are not the same
  property; a condition that is non-fatal but lossy needs its own
  third category (here: a `DEGRADED` state plus a cumulative
  `data_gap_count`), distinct from both "closes the stream" and
  "nothing happened."
- **A blanket `except Exception: pass` is the same defect class
  regardless of which lifecycle method it appears in.** The Phase
  3.2 lessons above already cover this for a storage rollback path;
  this correction found the identical anti-pattern in a live
  connection's `close()`. The fix is the same general shape every
  time: narrow the suppression to a SPECIFICALLY recognized, already-
  classified condition (here, "exception originates from the vendor
  package itself," reusing the exact helper already used for that
  classification elsewhere in the same adapter), and let anything
  else propagate, with a `finally` clause (not the `except` body)
  responsible for guaranteeing a coherent end state.

### Test-double fidelity and "narrow enough" shutdown/cleanup-exception lessons (from the Phase 4.2 real-package hardening pass, apply to every future vendor-record duck-typing boundary and every future shutdown/cleanup exception policy)

A second independent audit of the corrected Phase 4 artifact found
that every one of its four remaining defects was a defect the Phase 4
correction's OWN test doubles had been too permissive to expose --
not a new category of mistake, but the same category (an unverified
or insufficiently narrow assumption) hiding one layer deeper, past a
correction pass that had already fixed the previous round of these.
Durable lessons, on top of (not replacing) every lesson above:

- **A fake that always uses the most convenient built-in type can hide
  an `isinstance` regression that a less convenient but equally valid
  real-world type would expose immediately.** Every quote test double
  in this adapter used a plain Python `list` for Databento's `levels`
  field, so `isinstance(levels, (list, tuple))` always passed in
  testing -- even though the real vendor's documented array type for
  this exact field is indexable but NOT a `list`/`tuple` subclass. The
  generic version of this lesson: when a test double stands in for a
  vendor structure whose exact TYPE (not just its attribute shape)
  matters to the code under test, at least one test double must
  deliberately use a type that is duck-type-compatible but genuinely
  NOT whatever convenient built-in the implementation might be
  tempted to special-case against -- otherwise "every test passes"
  proves nothing about whether the implementation special-cases a
  built-in type the real vendor object doesn't actually subclass.
- **"Narrower than a blanket swallow" is not automatically "narrow
  enough" -- each narrowing has to be re-measured against what it
  still permits, not just against what it replaced.** The Phase 4
  correction's own narrowing of `close()` (from "suppress any
  exception" to "suppress any exception whose module starts with
  `databento`") was still audited as too broad a second time: a
  vendor-origin exception is not automatically a harmless one. A
  correction that narrows a scope should still be read, on its own,
  for whether the new scope is _conceptually_ correct (does every
  exception it still catches represent the EXACT condition the catch
  is meant to handle?), not merely checked against the old, cruder
  scope it replaced.
- **"Best-effort cleanup of a resource already known to be broken" is
  a genuinely different exception-handling policy than "normal,
  deliberate shutdown of a resource assumed healthy" -- even when both
  call the same underlying vendor method.** Stopping the OLD client
  during a mid-stream reconnect (§4) deliberately absorbs a WIDER set
  of vendor-origin exceptions than `close()` itself (§3) does, because
  forcing a hard failure out of cleanup of a connection the caller
  already knows is broken would convert one already-observed transient
  failure into an unrelated second one -- whereas `close()` is called
  on a connection with no such prior excuse. Reusing one lifecycle
  method's exception-classification HELPER (`_is_databento_exception`)
  across two call sites with deliberately different POLICIES (what to
  do once that classification is known) is correct; collapsing both
  call sites to the same policy because they call the same underlying
  method is not.
- **A duck-typed record-classification function and the normalization
  function it feeds must be updated TOGETHER, or the classifier
  silently routes a now-supported record shape to "unknown" rejection
  instead of to the handler that was just taught to read it.** Adding
  support for a second top-of-book representation (flat `*_00`
  properties, with no `levels` attribute at all) to the quote-field
  extraction helper, without also teaching `_classify_record` that a
  record exposing ONLY those flat properties is still a `QUOTE`, left
  such a record silently reclassified as `"unknown"` and rejected
  before the new extraction logic was ever reached -- caught only by
  actually running the new test against the real classification path,
  not by reasoning about the extraction helper in isolation. Any
  change to what shapes a normalization function can read must be
  paired with a check of whether an earlier, upstream classification
  step already decides which records are even offered to it.
- **An offline-only, no-network, no-API-key check against a real
  vendor package's own class objects (via `dir()`/`inspect`, never
  instantiation) is cheap, safe to run in CI, and catches a real class
  of defect no fake can: a published API's own shape silently changing
  out from under an adapter's duck-typing assumptions.** Extending the
  existing `Live`/`Historical` constructor-signature check to also
  introspect the real record classes' attribute names
  (`TradeMsg.sequence`, `Mbp1Msg`'s top-of-book attributes,
  `OhlcvMsg.volume`, etc.) costs nothing when the package is absent
  (skipped outright, `pytest.importorskip`) and directly confirms or
  refutes a specific, previously-unverifiable assumption the moment it
  runs somewhere the package IS installed -- this pattern should be
  the default for any future vendor-record duck-typing boundary, not
  something added only after an audit specifically asks for it.

### Full regression requirement before completion of any phase or correction

1. Run the full existing regression suite.
2. Run all new tests.
3. Run manual adversarial verification.
4. Inspect the final diff.
5. Inspect runtime/system-health output.
6. Inspect package contents.
7. Confirm no future-phase scope leaked in.
8. Confirm no secrets or local environment artifacts are packaged.
9. Confirm documentation matches the actual implementation.
10. Confirm previously fixed bugs remain fixed.

### Final self-review pass before delivery

Before returning a ZIP, a completion report, or declaring a phase
complete, do a separate pass that intentionally tries to disprove "this
is ready" rather than just confirming "I satisfied the prompt." Ask: what
did the prompt forget to ask? What assumptions were made? What would
another senior engineer attack? What could pass all the tests and still
be wrong? What configuration could be self-consistent but factually
incorrect? What failure mode could make the system lie about its own
state? If this surfaces a problem: fix it, add a regression test, rerun
the full suite, and only then deliver.
