# Olive AI

Olive AI is a Hudson Intelligence product being developed as an AI-powered
Nasdaq-100 futures (**NQ** / **MNQ**) market-intelligence and trade-signal
platform. It is designed around depth on one market rather than breadth
across many, and is intended to eventually produce probabilistic
`LONG` / `SHORT` / `NO_TRADE` theses with explicit uncertainty, not
guaranteed outcomes.

## Current Status

**Phase 4 — Real-Time Market Data**, corrected by a **Phase 4
correction pass** and then a narrower **Phase 4.2 real-package
hardening pass** (both below), is now implemented, building on
**Phase 3 — Historical Market Data** (final and consolidated, below).

Olive AI can now open a live streaming connection to a configured
provider (Databento) for its exact NQ/MNQ production universe, and
normalize vendor trade/quote/bar records into Olive's own `DataLabel.LIVE`
domain events, strictly on explicit opt-in. By default (and in every
test run), **no live provider is configured and live network access is
disabled** -- nothing in this build can open a live connection without
an operator deliberately setting `OLIVE_LIVE_PROVIDER=databento`,
`DATABENTO_API_KEY`, and `OLIVE_LIVE_NETWORK_ENABLED=true`. This is a
separate kill switch from Phase 3's historical one -- enabling either
never enables the other. See `docs/realtime_data.md` for the full
write-up, including this build's known limitations (the `databento`
package could not be installed in this build sandbox, so its
compatibility was verified only against duck-typed fakes matching its
documented interface -- see that document's "Known limitations"
section).

The **Phase 4 correction pass** is a single consolidated correction to
the originally delivered Phase 4 build, following an independent audit
of the actual delivered artifact against Databento's documented 0.87
Live API -- still Phase 4, not Phase 4.1 or a new phase. The audit
found: the live client was constructed with an unsupported `dataset`
kwarg and then had `.start()` called before synchronous iteration
(both contradicting the documented API, the second of which the real
client raises `ValueError` for); Databento's raw-symbol decade-reuse
ambiguity (`NQZ6` means a different contract in 2026 vs. 2036) was left
exploitable at the *live* layer -- unlike Phase 3's historical adapter,
nothing authoritatively proved a subscribed contract's full-year
identity before trusting its live `SymbolMappingMsg`; two Phase 3
regression tests had regressed back to an environment-dependent
`pytest.skip` CLAUDE.md and the original Phase 4 prompt both already
prohibited; the fatal-`ErrorMsg` code list omitted two documented fatal
conditions (`INTERNAL_ERROR`, `REPLAY_DATA_AGED_OUT`); a genuine,
documented data-loss condition (`SKIPPED_RECORDS_AFTER_SLOW_READING`)
was not surfaced at all; `reconnect_count`'s documentation promised
"successful reconnections only" while the implementation also counted
failed initial-connect attempts; `close()` used a blanket
`except Exception: pass`; and Olive's own computed receive timestamp
was discarded instead of being attached to every live event. All fixed
-- including a new pre-connection, point-in-time symbology-resolution
identity proof (reusing Phase 3's already-hardened resolver) that
rejects a stream before it ever opens if any subscribed contract's
full-year identity cannot be authoritatively confirmed for its own
contract month, or if two subscribed contracts collide on the same
resolved instrument ID -- with 40 new regression tests (1683 passed, 3
environment-dependent skips for genuinely optional real-package checks,
up from the original delivery's 1644 passed / 2 skipped) and manual
adversarial reproduction against the finished code. See
`docs/realtime_data.md` §8/§11 for the corrected mechanism and
`CLAUDE.md`'s streaming/live-connection lessons for the durable
takeaways.

The **Phase 4.2 real-package hardening pass** is a second, narrower
correction issued after a further independent audit of the corrected
Phase 4 artifact -- still Phase 4, not a new phase; `CLAUDE.md` and
both correction prompts explicitly prohibit a "Phase 4.1"/"Phase 5."
The audit found four remaining real-Databento compatibility/lifecycle
gaps the Phase 4 correction's own test doubles had been too permissive
to expose: MBP-1 quote extraction still gated `levels[0]` on
`isinstance(levels, (list, tuple))` and silently fell back to reading
`bid_px`/`ask_px`/`bid_sz`/`ask_sz` off the whole `Mbp1Msg` record
(fields that do not exist there) when that check failed, because every
test double used a plain Python `list` for `levels` -- fixed to prefer
Databento 0.87's documented flat `bid_px_00`/`ask_px_00`/`bid_sz_00`/
`ask_sz_00` properties, and otherwise treat `levels` as genuinely
indexable (narrow `try`/`except`, never `isinstance`), rejecting with
an Olive-owned error when neither representation is usable; `close()`
still suppressed *any* exception merely because its class module
started with `databento`, which is still too broad a vendor-origin
exception is not automatically harmless -- fixed to a three-way split
(recognized-benign shutdown conditions suppressed; unexpected vendor
exceptions translated into a new `LiveProviderShutdownError` and
raised, never discarded; non-vendor exceptions always propagate); the
mid-stream reconnect path replaced the old client with a new one
without first attempting to stop it, risking a leaked connection --
fixed with best-effort old-client cleanup (deliberately more
permissive than `close()`'s own policy) before the new client is
established; and the real-package offline compatibility check only
inspected `Live`/`Historical` constructor signatures -- extended to
also introspect the real `TradeMsg`/`Mbp1Msg`/`OhlcvMsg`/
`SymbolMappingMsg`/`ErrorMsg`/`SystemMsg`/`BidAskPair` record classes'
attribute names (still no network, no API key) whenever the real
package is installed. All fixed, with 19 new regression tests (1702
passed, 11 environment-dependent skips for genuinely optional
real-package checks, up from the Phase 4 correction's 1683 passed / 3
skipped) and the full non-regression checklist re-verified intact. See
`docs/realtime_data.md` §6/§11/§14 for the corrected mechanisms.

Live events (trades, quotes, one-second bars) are never persisted and
never written to the Phase 3 historical Parquet store -- Phase 4 is a
sibling subsystem to Phase 3, not an extension of it. A separate
`LiveDataError` hierarchy, a separate `RealTimeMarketDataProvider`
interface, and a separate `RealTimeDataService` gate pipeline (whole-
domain and per-contract production-tradability checks, provider
configuration, and the live network opt-in, in that order, before any
connection is attempted) exist alongside Phase 3's equivalents without
changing them. A real `Real-time market data` health check (mirroring
Phase 3's own) never opens a connection merely to report
`NOT_CONFIGURED` / `CONFIGURED` / `ERROR`.

Olive still has **no feature engine, no predictive models, no
strategies, no signals, and no web interface.** Olive does not predict
NQ yet; it understands the instruments it will eventually trade
(Phase 2), can build a historical record of their prices (Phase 3),
and can now observe their live activity (Phase 4).

### Phase 3 status (preserved)

**Phase 3 — Historical Market Data**, corrected by a **Phase 3.1
safety & integrity hardening pass**, a **Phase 3.2 final historical
integrity correction**, a **Phase 3.3 canonical historical storage
finalization**, a **Phase 3.3 QA compliance rework**, and the **Phase 3
final completion pass** (each a process correction to the same Phase
3 delivery -- not a new phase), following independent review of the
previously delivered build each time. Phase 3 is now considered
complete and consolidated; see `olive-phase3-final.zip`'s completion
report for the final compliance ledger.

Olive AI can now fetch, validate, and durably store historical OHLCV
bars for its exact NQ/MNQ production universe from a configured
provider (Databento), strictly on explicit request. By default
(and in every test run), **no provider is configured, network access
is disabled, and the spending limit is zero** -- nothing in this build
can download real market data or incur cost without an operator
deliberately setting `OLIVE_HISTORICAL_PROVIDER=databento`,
`DATABENTO_API_KEY`, `OLIVE_HISTORICAL_NETWORK_ENABLED=true`, and a
non-zero `OLIVE_HISTORICAL_MAX_REQUEST_COST_USD`. See
`docs/historical_data.md` for the full write-up, including the
Phase 3.1 (§16), Phase 3.2 (§17), and Phase 3.3 (§18) correction
summaries and this build's known limitations (the `databento`/
`pyarrow` packages could not be installed in this build sandbox -- see
that document's §14).

The Phase 3.1 pass fixed several substantive findings from independent
review without changing Phase 3's scope: a `Settings` construction
path that could silently bypass the network kill switch; a vendor
error-translation path that could leak the API key through its own
message text; Databento's documented raw-symbol year-reuse problem
(`NQZ6` means a different contract in 2016/2026/2036), now guarded by
a symbology-resolution identity-safety gate before any paid fetch; a
non-transactional Parquet+manifest write pair; a manifest that was
written but never verified on read; and roughly a dozen smaller
public-boundary hardening fixes. See `docs/historical_data.md` §16 for
the full list.

The Phase 3.2 pass fixed further findings discovered by adversarially
re-testing the *actual delivered* Phase 3.1 build, still without
changing Phase 3's scope: a response row missing `instrument_id` was
silently given a fabricated fallback value instead of failing closed;
the symbology identity-safety gate's own date-range math had an
off-by-one that could leave a cross-day, intraday-end-time request
under-protected against exactly the raw-symbol decade-reuse problem it
exists to catch; a bar's `tick_size`/`provider`/`provider_raw_symbol`
were never cross-checked against Olive's production-validated
instrument or the provider actually servicing the request; and
`write_bars` could legitimize previously-unverified or corrupt
existing data by skipping the same manifest/checksum check `read_bars`
already enforced. See `docs/historical_data.md` §17 for the full list,
and the corrective ZIP's completion report for the complete
accounting.

The Phase 3.3 pass fixed further findings discovered by adversarially
re-testing the *actual delivered* Phase 3.2 build, still without
changing Phase 3's scope: a pre-existing identical duplicate in
storage could cause `write_bars` to crash constructing its own result
object *after* a successful commit; a misplaced partition (wrong
calendar month) could pass every existing check and be silently
filtered into an innocent-looking empty read result, even against a
manifest forged to correctly claim the right directory; one partition
could hold internally incoherent bars (mixed `tick_size`/
`provider_raw_symbol`/`provider_instrument_id`); `read_bars` did not
reject an orphan manifest or validate duplicate canonical keys the way
`write_bars` already did; a manifest's own descriptive fields were
never cross-checked against what was actually decoded from its
partition; and Databento symbology instrument-ID resolution did not
strictly validate value format/entry shape before a paid fetch. See
`docs/historical_data.md` §18 for the full list, and the corrective
ZIP's completion report for the complete accounting.

The **Phase 3.3 QA compliance rework** is a correction to the Phase 3.3
delivery and to the completion *process* itself -- still Phase 3.3, not
a new phase. Independent review reproduced the Phase 3.3 baseline as
genuinely strong, but found that completion had been declared without
every standing QA boundary actually being exhaustively re-tested:
manifest integer fields were not strict against Python's `True == 1`/
`1.0 == 1` equality traps; `record_count == 0` was not rejected as the
impossible state it is; malformed nested Databento symbology containers
(e.g. `not_found=123`) leaked a raw `TypeError` instead of
`ProviderSymbologyError`, because `value or []` only replaces falsy
values, never type-checks; and `HistoricalReadResult`/
`HistoricalWriteResult` still had one unconstrained impossible-state
combination each. All four fixed, with 74 new regression tests (1142
passed, 2 skipped total) and manual adversarial reproduction against
the finished code. See `docs/historical_data.md` §19 for the full list
and `CLAUDE.md`'s new completion-claim-rule lesson, and the corrective
ZIP's completion report for the complete compliance-ledger accounting.

The **Phase 3 final completion pass** is the consolidated close-out of
Phase 3 -- still Phase 3, not a new phase. It fixed the last four
confirmed gaps in the QA-rework build, each one level deeper than
anything tested before: manifest `last_written_at_utc` was written but
never validated on read; manifest `requested_start_utc`/
`requested_end_utc` were each individually validated but never checked
against each other; Databento `not_found`/`partial` were validated as
containers but never member-by-member (so `not_found=[123]` passed
silently); and the Databento `result` mapping was only ever validated
at the requested symbol's own slice, letting a malformed sibling entry
through. All four fixed, with 42 new regression tests (1184 passed, 2
skipped total) and manual adversarial reproduction against the
finished code. `d0`/`d1` symbology fields were deliberately left
unvalidated -- nothing reads them, and the properties they would
protect are already independently guaranteed elsewhere. See
`docs/historical_data.md` §20 for the full list and `CLAUDE.md`'s
member-level/sibling-entry validation lessons, and the final ZIP's
completion report for the complete compliance-ledger accounting.

Phase 2 (below) is preserved as originally written.

### Phase 2 status (preserved)

**Phase 2.5 — Final Public Validator Hardening** (following a fifth
external code review of Phase 2; the final Phase 2 hardening pass).

Olive AI now knows what NQ and MNQ actually are -- contract economics,
quarterly contract identity, the expiration/roll calendar, and the
regular Globex session schedule -- but still has **no market data, no
predictive models, no strategies, no signals, and no web interface.**
Olive does not predict NQ yet; it only understands the instruments it
will eventually trade.

Phase 2.1 strengthened that understanding without adding scope: contract
and instrument construction now validate their own invariants no
matter how they're built, contract navigation rejects cross-instrument
misuse, the expiration/roll calendar cross-validates its own declared
metadata against its data, and the production health check rejects a
tradable registry that doesn't match Olive's exact `{NQ, MNQ}`
universe.

Phase 2.2 closed the gaps a second, broader audit found: instrument
economics and point-value conversions reject non-finite
`Decimal`/`float` values (`NaN`/`Infinity`/`-Infinity`) instead of
leaking a raw `decimal.InvalidOperation`; the expiration/roll calendar
and instrument registry validate themselves fully even when
constructed directly (not only via their JSON loaders); the customary
roll-date invariant is exact (anchored to the nominal third Friday,
not merely "some Monday before expiration"); and every public
*date/datetime-accepting* function across the futures domain rejects
the wrong type with a domain error instead of a raw
`AttributeError`/`TypeError`. That date/datetime scope was real but
narrower than it sounded: a public function's *other* parameters
(`instrument`, `contract`, `cycle_calendar`, a config loader's
`config_path`) were still taken on faith as of Phase 2.2 and could
still leak a raw `AttributeError`/`TypeError` for the wrong object
type -- a gap a third audit found and Phase 2.3 (below) closes.

Phase 2.3 closes the remaining public-API gaps a third audit found:
`ContractCycleDates` and `nominal_customary_roll` now require a plain
`date`, deliberately rejecting a `datetime` (which previously either
slipped through silently or raised a raw cross-type `TypeError`);
`FuturesInstrument.contract_months` validates its container type
before iterating it (`contract_months=True` previously raised a raw
`TypeError: 'bool' object is not iterable`); JSON-sourced contract
months must be true integers (`3.0` previously passed silently,
because `ContractMonth(3.0) == ContractMonth.MARCH` in Python's enum
lookup); every public function in `app/futures/contracts.py`,
`app/futures/roll.py`, and `app/futures/sessions.py` now validates its
`instrument`/`contract`/`cycle_calendar`/`cycle` parameters via shared
validators instead of leaking a raw `AttributeError` the first time it
touches one of that object's attributes; both JSON config loaders
(`load_instrument_registry`, `load_cycle_date_calendar`) now accept
any path-like `config_path` (`Path`, `str`, or `os.PathLike`) instead
of requiring an actual `Path`; and Decimal arithmetic that can overflow
the active context even on individually-finite operands (e.g.
`tick_size * multiplier` or a tick/point conversion on an extreme
value) is now wrapped so a `decimal.Overflow` becomes a domain error,
never a raw stdlib exception.

Phase 2.3's hardening made every public function fail closed for the
*wrong object type* -- but, as a fourth audit demonstrated, that was a
narrower guarantee than "Olive's production domain is correct." A
structurally self-consistent but factually wrong NQ definition
(`multiplier=2, tick_size=0.25, tick_value=0.50, contract_months=[3]`
-- internally valid since `0.25 x 2 == 0.50`) and a cycle-date calendar
truncated down to a complete-but-incomplete 2025-only snapshot both
still reported `Futures domain: CONFIGURED`, because nothing checked
that the loaded NQ/MNQ entries actually described the real product, or
that Olive's required 2026-2028 source-backed coverage (including the
June 2026 holiday exception) was still present.

**Phase 2.4** closes that gap with a new, reusable production-domain
validation layer, `app/futures/validation.py`, deliberately separate
from the GENERIC `FuturesInstrument` / `FuturesInstrumentRegistry` /
`CycleDateCalendar` validation those classes already perform (which
stays generic, not NQ/MNQ-specific, on purpose): it checks that the
loaded NQ and MNQ instruments match Olive's canonical Phase 2
financial specification exactly (root symbol, exchange, underlying,
currency, multiplier, tick size, tick value, settlement type, and the
full `{3, 6, 9, 12}` quarterly set), and that the loaded cycle-date
calendar still contains Olive's required 2025-2028 source-backed
baseline -- including every individual anchor's exact expiration/roll
dates, not just year-level coverage -- while still allowing a
legitimate future update to add years beyond 2028 without any change
to this module. `app.health._check_futures_domain()` now calls this
validator, so a structurally valid but factually wrong production
configuration reports `Futures domain: ERROR`, never `CONFIGURED`.

Phase 2.4's own three new public validators
(`validate_olive_tradable_registry`, `validate_olive_cycle_calendar`,
`validate_olive_futures_domain`) were, themselves, a repeat of the
pattern Phase 2.3 had just fixed everywhere else: they trusted their
`registry`/`calendar` arguments' *types* without checking them,
leaking a raw `AttributeError` for `None`, a primitive, a different
Olive domain object passed by mistake, or a hand-built duck-typed
stand-in. **Phase 2.5** closes that gap by applying the same shared
validators already used throughout the rest of the futures domain
(`_require_registry`, new in `app.futures.registry`; the
already-existing `_require_cycle_calendar` in `app.futures.calendar`)
to these three functions, with no change to Phase 2.4's canonical
NQ/MNQ specifications, calendar baseline, or health-reporting
behavior. See `docs/futures_domain.md` for the full write-up.

## Long-Term Purpose

Olive AI's eventual goal is to continuously analyze Nasdaq-100 futures
and produce calibrated, explainable `LONG` / `SHORT` / `NO_TRADE`
trade theses -- with entry zones, invalidation levels, targets, and
supporting/opposing evidence -- backed by real feature engineering,
validated machine-learning models, and a signal-fusion engine. It is
developed in 14 sequential phases; see `docs/architecture.md` for how
Phases 1-2 fit into that plan.

## Current Capabilities

Foundation (Phase 1):

- A typed, environment-driven configuration system (`app/config.py`)
- Centralized, duplicate-safe logging setup (`app/logging_config.py`)
- A structured, truthful system-health model (`app/health.py`)
- An application entry point that starts, reports status, and exits
  cleanly (`main.py`)

Futures domain (Phase 2) -- see `docs/futures_domain.md` for full detail:

- Typed NQ/MNQ instrument definitions with validated contract economics
  (multiplier, tick size, tick value) using `Decimal`, not floats
- Unambiguous quarterly contract identity and CME-style display codes
  (e.g. `NQZ6`), with year-rollover-safe next/previous contract helpers
- An official-vs-calculated-nominal expiration/roll calendar (source-backed
  CME dates for 2025-2028, with a clearly labeled fallback outside that range)
- Calendar-based (never liquidity-based) customary lead-contract resolution
- The regular CME Globex weekly session schedule in `America/Chicago`
  (DST-safe via `zoneinfo`), trade-date mapping, and expiring-contract
  tradability checks
- A real futures-domain health check (registry + calendar must actually
  load and validate, and the tradable roots must exactly match `{NQ, MNQ}`,
  before "Futures domain" reports `CONFIGURED`)
- (Phase 2.1) Self-validating contract/instrument construction,
  cross-instrument navigation protection, strict integer tick-count
  semantics, and calendar metadata cross-validation -- all domain
  errors fail closed with explicit, specific exception types, never a
  bare `ValueError`/`KeyError`/`calendar.IllegalMonthError`
- (Phase 2.2) Finite-only Decimal economics and point-value conversions,
  a fully self-validating calendar and instrument registry regardless
  of construction path, an exact (not merely "a Monday") customary
  roll-date invariant, and consistent domain-error behavior for every
  malformed *date/datetime* input across the futures domain -- never a
  bare `decimal.InvalidOperation`/`TypeError`/`AttributeError`
- (Phase 2.3) A strict date-only invariant (`ContractCycleDates`,
  `nominal_customary_roll` reject `datetime`), container-type-safe
  instrument construction, true-integer-only JSON contract months,
  path-like config loader parameters (`Path`/`str`/`os.PathLike`), and
  shared object validators applied to every public
  `instrument`/`contract`/`cycle_calendar`/`cycle` parameter across
  `contracts.py`/`roll.py`/`sessions.py` -- no public function in the
  futures domain leaks a raw `AttributeError`/`TypeError` for a
  wrong-typed argument of any kind, and Decimal arithmetic overflow on
  individually-finite operands fails closed as a domain error, never a
  raw `decimal.Overflow`
- (Phase 2.4) A new, reusable `app/futures/validation.py` production-
  domain integrity layer: validates the loaded NQ/MNQ instruments
  against Olive's canonical Phase 2 financial specification exactly
  (not merely that they're internally self-consistent), and validates
  the loaded cycle-date calendar still contains Olive's required
  2025-2028 source-backed baseline, anchor by anchor -- a structurally
  valid but factually wrong NQ/MNQ definition, or a calendar missing
  required official coverage, now reports `Futures domain: ERROR`
  instead of `CONFIGURED`. The generic domain objects
  (`FuturesInstrument`, `FuturesInstrumentRegistry`,
  `CycleDateCalendar`) remain deliberately generic; this new layer is
  the one place Olive's specific production requirements live
- (Phase 2.5) The three Phase 2.4 production validators
  (`validate_olive_tradable_registry`, `validate_olive_cycle_calendar`,
  `validate_olive_futures_domain`) now validate their own
  `registry`/`calendar` parameters' types first, via the same shared
  validators used throughout the rest of the futures domain -- closing
  a repeat of the Phase 2.3 defect class that was found in this
  brand-new module. No raw `AttributeError` escapes for a malformed
  argument, a wrong-kind Olive domain object, or a hand-built
  duck-typed stand-in; Phase 2.4's canonical specs, calendar baseline,
  and health behavior are unchanged

A full test suite covers both areas (configuration, health, startup,
instruments, registry, contracts, calendar, roll, sessions, and
production-domain validation), including adversarial regression tests
for every Phase 2.1, Phase 2.2, Phase 2.3, Phase 2.4, and Phase 2.5
finding.

Historical market data (Phase 3) -- see `docs/historical_data.md` for
full detail:

- A provider-independent historical-data interface with a safe default
  (`UnconfiguredHistoricalProvider`, used whenever no provider is
  configured) and a real Databento adapter (`DatabentoHistoricalProvider`)
  that only ever requests exact contracts via `stype_in="raw_symbol"`
  -- never Databento's parent (`NQ.FUT`) or continuous (`NQ.c.0`)
  symbology -- for `ohlcv-1s`/`1m`/`1h`/`1d` schemas only
- Typed, self-validating `HistoricalBarRequest`/`HistoricalBar` domain
  objects storing prices as exact integer tick counts (never
  float/bare Decimal), with a `data_label` that can only ever be
  `HISTORICAL` in this build
- An explicit `HistoricalFetchStatus` for every outcome (success,
  empty, not configured, each specific rejection, failure) -- never
  `None`/an empty list/an exception alone
- `HistoricalDataService`, which gates every fetch through Olive's
  production tradable-domain check (before any provider call),
  request validation, provider configuration, an explicit network
  opt-in, and a `Decimal`-compared cost limit -- in that order
- A deterministic, atomic, idempotent local Parquet storage layer
  (`HistoricalBarStore`) with a per-partition provenance manifest
  (including a SHA-256 checksum) and no secrets ever written to disk
- A real `Historical market data` health check that never calls any
  provider network method, reporting `NOT_CONFIGURED` (the honest
  default), `CONFIGURED`, or `ERROR` for broken configuration

Phase 3.1 correction (following independent review of the delivered
Phase 3 build) -- see `docs/historical_data.md` §16 for full detail:

- `Settings` now validates and canonicalizes its own Phase 3 fields in
  `__post_init__` -- previously `Settings(historical_network_enabled="false")`
  (a truthy string) could silently bypass the network kill switch when
  constructed directly rather than through `load_settings()`; a
  relative `historical_data_dir` now resolves against Olive's project
  root rather than the process's current working directory
- The Databento adapter never echoes a vendor exception's own message
  text into anything it raises (Databento's documented invalid-auth
  error can itself contain the API key) -- only fixed, generic,
  per-category messages, with the exception chain severed
- A new symbology-resolution identity-safety gate runs before any paid
  fetch, protecting against Databento's documented raw-symbol reuse
  across years (`NQZ6` means a different contract in 2016/2026/2036)
- Provider volume is converted with a safe integral check that rejects
  a fractional value instead of silently truncating it; a provider
  row's own data-quality failures and a provider's malformed
  `estimate_cost`/`fetch_bars` return values now fail closed as
  structured errors rather than leaking or being mistaken for success
- `HistoricalBarStore.write_bars`'s Parquet+manifest write is now
  transactional (single-partition and cross-partition rollback on
  failure); `read_bars` now verifies the manifest's identity and a
  freshly-recomputed checksum before trusting any partition, and
  rejects an unsupported schema version or silently-coerced
  bool/float/string contract year/month
- `HistoricalDataService`/`build_historical_provider` constructors now
  validate every argument with Olive's own error type, never a raw
  `TypeError` or a deferred `AttributeError`
- Corrected ZIP packaging: this archive's contents sit directly at the
  archive root, matching the Phase 2.5 convention (the delivered
  `olive-phase3.zip` had mistakenly nested everything under an
  `olive-ai/` prefix)

Phase 3.2 correction (following a further independent review of the
*actually delivered* Phase 3.1 build) -- see `docs/historical_data.md`
§17 for full detail:

- A Databento response row missing `instrument_id` now fails closed
  (`ProviderResponseIdentityError`) instead of silently having its
  provenance fabricated from the symbology-resolved expected value
- The symbology identity-safety gate's date-range math now correctly
  covers every instant of a cross-day request with an intraday end
  time (e.g. ending `...T00:30Z`) -- the previous projection could
  leave such a request's symbology check covering none of the day it
  actually needed to protect
- `normalize_and_validate_bars` now cross-checks every bar's
  `tick_size` against Olive's production-validated instrument and its
  `provider`/`provider_raw_symbol` against the provider actually
  servicing the request -- a bar can be internally self-consistent
  while still being production-wrong, and is now never stored on that
  basis alone
- `write_bars` now verifies an existing partition's manifest/checksum
  before merging onto it (previously only `read_bars` did), and
  rejects existing stored data that already contains conflicting
  duplicates, rather than silently legitimizing either
- `HistoricalWriteResult`/`HistoricalReadResult` are now
  self-validating (non-negative counts, `new_records <= total_records`,
  `bars` normalized to real `HistoricalBar` instances); a rollback
  failure during either single- or cross-partition recovery is no
  longer silently swallowed; `HistoricalBar.conflicts_with` validates
  its argument; the Databento adapter's injected-client seam is
  validated at construction; `HistoricalBarStore`'s root is resolved
  CWD-independently even on direct construction; `HistoricalDataService`
  validates `write_bars`'s own return type and translates an expected
  storage `OSError` into a structured `FAILED` result
- Permanent shipped regression coverage
  (`tests/test_historical_data_storage_transaction.py`) for every
  storage-transaction behavior that previously existed only in an
  unshipped scratch-space harness

Phase 3.3 correction (following a further independent review of the
*actually delivered* Phase 3.2 build) -- see `docs/historical_data.md`
§18 for full detail:

- A pre-existing partition holding two identical copies of a bar now
  canonicalizes to one record *before* `write_bars` computes any
  quantity that feeds `HistoricalWriteResult`'s construction, making a
  post-commit construction failure structurally impossible rather than
  merely less likely
- `read_bars` now enforces the same canonical-duplicate policy
  `write_bars` already did (identical collapses to one; conflicting
  raises), including as defense in depth on its final cross-partition
  result, and now rejects an orphan manifest (manifest present,
  parquet missing) the same way the reverse orphan direction already
  was
- A partition's `year=YYYY/month=MM` directory identity is now
  verified two independent ways: the manifest's own new
  `partition_year`/`partition_month` fields against the directory, and
  every individual loaded bar's own `ts_event` against that same
  directory -- the second layer catches a misplaced partition even if
  its manifest is forged to correctly claim the right directory
- One partition must now stay internally coherent on `tick_size`/
  `provider_raw_symbol`/`provider_instrument_id`/`data_label`/
  `schema_version` -- enforced on every write and every read, not just
  implied by the write-side grouping key
- A manifest's own descriptive fields (`record_count`, first/last
  timestamp, `provider_raw_symbol`, `tick_size`, ...) are now
  cross-checked against what was actually decoded from its partition,
  not only its raw-bytes checksum
- Databento symbology instrument-ID resolution now strictly validates
  both each mapping entry's shape and the resolved value's format
  (a real, non-empty, ASCII-digit, strictly-positive string) before
  any paid fetch
- `HistoricalBar`'s tick-count/volume fields now enforce
  `ARROW_INT64_MAX = 2**63 - 1`, the exact signed-64-bit bound Olive's
  own Parquet schema declares for those columns; `_bars_to_table`'s
  actual Arrow conversion calls now translate an expected failure into
  `HistoricalStorageError`, narrowly scoped so a genuine Olive bug in
  the attribute-gathering loop above them still propagates unmodified
- A new, entirely offline real-`databento`-package compatibility test
  introspects the installed package's documented interface without
  ever making a network call, so installing the real package proves
  this adapter's assumed interface still holds
- Permanent shipped regression coverage for every issue above, added
  to `tests/test_historical_data_storage_transaction.py` (now 53
  tests, up from 26)

Real-time market data (Phase 4) -- see `docs/realtime_data.md` for full
detail:

- A provider-independent real-time-data interface with a safe default
  (`UnconfiguredLiveProvider`, used whenever no provider is configured)
  and a real Databento live adapter (`DatabentoLiveProvider`) that
  subscribes only to Olive's exact, subscription-validated NQ/MNQ
  contracts, normalizing Databento's trade/quote/OHLCV records into
  Olive's own `LiveTrade`/`LiveQuote`/`LiveBar` domain events
- Typed, self-validating live event objects storing prices as exact
  `Decimal` values tick-aligned to the production instrument's own
  tick size, each independently enforcing `data_label is DataLabel.LIVE`
  in its own construction, regardless of which code path built it
- A pre-connection, point-in-time symbology-resolution identity proof
  (Phase 4 correction): before any live connection opens, every
  subscribed contract's full-year identity is authoritatively resolved
  against Databento's metadata endpoint, scoped to that contract's own
  calendar month -- Databento's raw-symbol decade-reuse ambiguity
  (`NQZ6` is `NQ`'s December contract in 2026 *and* 2036) is resolved
  before it can ever matter, a contract not yet listed for its own
  month fails closed, and two contracts colliding on the same resolved
  instrument ID are rejected outright. Only then does exact-contract-
  identity enforcement continue at the live-mapping layer: unmapped
  instrument data is rejected (counted, not raised), a live
  `SymbolMappingMsg` that contradicts a contract's own pre-verified
  identity is rejected closed, and a contradictory remapping of the
  same instrument ID to a different contract fails the stream closed
- Bounded-exponential-backoff automatic reconnection on transient
  connection failures (auth/permission failures are never retried),
  with an injectable sleep function so reconnect-backoff behavior is
  fully regression-tested without a single real sleep -- and truthful
  reconnect telemetry (Phase 4 correction): `reconnect_count` counts
  only successful re-establishments of a *previously lost* connection,
  never an initial-connect retry, which is tallied separately in
  `reconnect_attempts_total`
- Explicit data-loss surfacing (Phase 4 correction): a documented
  `SKIPPED_RECORDS_AFTER_SLOW_READING` condition transitions the stream
  to a `DEGRADED` state and increments a cumulative `data_gap_count`,
  clearing back to `CONNECTED` on the next successfully accepted event
  -- Olive never continues as though a stream that lost data stayed
  fully healthy. `close()` now only suppresses a narrowly recognized
  vendor-side shutdown condition, never an unexpected exception (Phase
  4 correction; no more blanket `except Exception: pass`)
- Three independently preserved timestamps on every live event (Phase
  4 correction): the exchange/event time (`ts_event`), Databento's own
  receive time (`ts_recv`, when the schema supplies one), and Olive's
  own local wall-clock receive time (`olive_received_at`) -- previously
  computed but silently discarded
- `RealTimeDataService.open_stream`, which gates every stream open
  through Olive's whole-domain and per-contract production-tradability
  checks (reusing Phase 2/Phase 3's existing validators, never
  duplicating them), provider configuration, and an explicit, Phase-3-
  independent network opt-in (`OLIVE_LIVE_NETWORK_ENABLED`) -- in that
  order, before any connection is attempted
- Session-aware staleness detection (`is_feed_unexpectedly_stale`) that
  reuses Olive's own Globex session schedule so a legitimately closed
  weekend or maintenance window is never misreported as an
  unexpectedly stale feed
- A real `Real-time market data` health check that never opens a
  connection, reporting `NOT_CONFIGURED` (the honest default),
  `CONFIGURED`, or `ERROR` for broken configuration

## Not Yet Implemented

The following are intentionally **not** part of Olive AI yet and will be
added in later phases:

- Feature engine (technical, order-flow, session, cross-market)
- Strategy framework
- Machine-learning prediction engine
- Signal fusion / `LONG` / `SHORT` / `NO_TRADE` generation
- Backtesting and market replay
- Paper trading
- Event intelligence and alerts
- Olive Web (the user-facing interface)

Olive AI's health report always reflects this truthfully at runtime --
it never claims a capability exists before it has actually been built.
Known Phase 2 limitations (no live volume/open-interest contract
selection, no complete holiday-aware session calendar, a finite
source-backed cycle-date table) are documented in `docs/futures_domain.md`.

## Requirements

- Python 3.11+

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Environment

Olive AI runs with safe defaults and requires no secrets to start. To
customize local settings:

```bash
cp .env.example .env
```

then edit `.env` as needed. `.env` is git-ignored and must never be
committed.

## Run

```bash
python main.py
```

This loads configuration, sets up logging, computes a truthful system
health snapshot, prints a status report, and exits with code `0`.

## Tests

```bash
pytest
```

## Security

- `.env` and other local secret files are excluded via `.gitignore`.
- `.env.example` contains only safe placeholder values (`DATABENTO_API_KEY=`
  is blank).
- No API keys, credentials, or secrets are ever written to source
  files, tests, logs, documentation, manifests, or Olive-generated
  error messages.
- Through Phase 2, Olive makes no network calls of any kind -- the
  futures domain reads only local, version-controlled JSON
  configuration.
- As of Phase 3, Olive can make a real network call to Databento, but
  only via `HistoricalDataService.fetch_and_store`, and only once every
  safety gate in `docs/historical_data.md` §9 has passed (provider
  configured with a real key, network explicitly enabled, a valid
  NQ/MNQ request, and a passing cost check). Importing any module,
  loading settings, running the test suite, running `main.py`, or
  computing system health never performs network I/O or incurs cost.
- As of Phase 4, Olive can open a real live connection to Databento,
  but only via `RealTimeDataService.open_stream`, and only once every
  safety gate in `docs/realtime_data.md` has passed (provider
  configured with a real key, a valid NQ/MNQ subscription, and live
  network access explicitly enabled via `OLIVE_LIVE_NETWORK_ENABLED`
  -- a switch kept entirely independent of Phase 3's historical one).
  Importing any module, loading settings, running the test suite,
  running `main.py`, or computing system health never opens a live
  connection.

## Development Roadmap

Olive AI is built in 14 sequential phases (Foundation, Futures Domain,
Historical Data, Real-Time Data, Market Intelligence, Strategies,
Prediction Engine, Fusion/Signal Engine, Backtesting, Paper Trading,
Olive Web, Advanced Intelligence, Always-On Operation, and Production
Validation). Each phase is implemented and tested before the next
begins. See `docs/architecture.md` for architecture details,
`docs/futures_domain.md` for the full Phase 2 futures-domain write-up,
`docs/historical_data.md` for the full Phase 3 historical-data
write-up, and `docs/realtime_data.md` for the full Phase 4 real-time-
data write-up.
