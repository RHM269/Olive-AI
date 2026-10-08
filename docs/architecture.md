# Olive AI — Architecture (through Phase 4)

This document describes Olive AI's architecture as it exists through
**Phase 4 — Real-Time Market Data**. Each phase's section below is
preserved as originally written at the time that phase (and its
corrective passes) shipped; a new section is appended for each
subsequent phase rather than rewriting that history. It intentionally
does not describe future phases' internals; see the project's phase
plan for what comes next.

## Scope of Phase 1

Phase 1 establishes the application shell: configuration, logging,
health reporting, an entry point, and a test suite. It implements no
market-data, futures-domain, feature, strategy, prediction, signal,
backtesting, paper-trading, alerting, or web functionality. Those are
explicitly out of scope until their respective phases.

## Module Map (Phase 1)

```
app/
├── __init__.py        Package marker only. No side effects on import.
├── config.py           Typed Settings, Environment, DataMode, load_settings()
├── logging_config.py   configure_logging(), get_logger()
└── health.py            HealthState, ComponentHealth, SystemHealth,
                          get_system_health(), format_health_report()

main.py                  Orchestration-only entry point
tests/                    pytest suite covering the above
```

## Startup / Data Flow

```
Environment Variables (+ optional .env)
            |
            v
      app.config.load_settings()
            |
            v
         Settings  ------------------->  app.logging_config.configure_logging()
            |                                        |
            v                                        v
  app.health.get_system_health()                 Logger ("olive")
            |
            v
  app.health.format_health_report()
            |
            v
         main.py  ---->  stdout status report, exit code
```

1. `main.py` calls `load_settings()`, which reads `APP_NAME`,
   `ENVIRONMENT`, `LOG_LEVEL`, and `OLIVE_DATA_MODE` from the process
   environment (optionally pre-populated from a local `.env` file),
   falling back to safe development defaults for anything unset. A
   value that is set but invalid raises `ConfigurationError`, and
   `main.py` fails closed (prints an error, exits `1`) rather than
   guessing.
2. `configure_logging()` sets up Olive's `"olive"` logger once per
   process, using the resolved log level. Calling it again (e.g. in
   tests, or if `main()` runs twice) updates the level without
   attaching a second handler.
3. `get_system_health()` builds an immutable, timestamped
   `SystemHealth` snapshot: `System` and `Configuration` are reported
   as real (`ONLINE` / `CONFIGURED`), and every other subsystem is
   reported as `NOT_IMPLEMENTED` with a note on which future phase
   will build it.
4. `format_health_report()` turns that structured snapshot into the
   CLI text `main.py` prints. This formatting step is deliberately
   separate from health computation so a future API or Olive Web page
   can reuse `SystemHealth` without re-deriving or re-parsing text.

## Health Semantics

`HealthState` distinguishes:

- `NOT_IMPLEMENTED` -- the component does not exist in the codebase
  yet (true for every subsystem but System/Configuration in Phase 1).
- `NOT_CONFIGURED` -- the component exists but is missing required
  configuration (not used yet in Phase 1, but reserved for e.g. a
  Phase 3+ data provider with no API key).
- `ONLINE` / `CONFIGURED` -- genuinely operational today.
- `DEGRADED` / `ERROR` -- reserved for future runtime-failure
  reporting.

This vocabulary is intentionally small and is expected to be reused,
not replaced, by later phases.

## Testing

- `tests/test_config.py` -- defaults, environment overrides,
  invalid-value handling, and immutability of `Settings`. Every test
  clears Olive's environment variables via `monkeypatch` and calls
  `load_settings(load_dotenv_file=False)` so results never depend on a
  developer's local `.env`.
- `tests/test_health.py` -- confirms `System`/`Configuration` report
  as operational, confirms every other Phase 1 component is honestly
  reported as not yet built, checks timestamp timezone-awareness,
  component-name uniqueness, and structured (non-string-only) output.
- `tests/test_main.py` -- startup smoke tests: successful run, fail-
  closed behavior on invalid configuration, and no duplicate log
  handlers across repeated `main()` calls.

## Architectural Boundaries (must hold for all future phases)

- **`main.py`** -- orchestration only. No trading, data, or model
  logic belongs here.
- **`app/config.py`** -- configuration only. No business logic.
- **`app/health.py`** -- status reporting only. No live calls to
  market-data providers or any other external service.
- **`app/logging_config.py`** -- logging setup only.

Future phases add new domain packages under `app/` (e.g. `futures/`,
`data/`, `features/`, `strategies/`, `prediction/`, `signals/`) rather
than growing these four modules into general-purpose dumping grounds,
and rather than placing that logic in web routes or `main.py`.

## Extension Philosophy

Phase 1 deliberately does not create empty placeholder packages for
Phases 2-14 (`futures/`, `data/`, `realtime/`, `features/`,
`strategies/`, `prediction/`, `signals/`, `events/`, `backtest/`,
`paper/`, `alerts/`, `web/`). Each is created when its phase actually
begins, with real contents, tests, and documentation -- not as
speculative scaffolding.

The two extension points Phase 1 guarantees are:

1. `get_system_health()` can flip any entry in `_NOT_YET_IMPLEMENTED`
   over to a real, operational `ComponentHealth` (or introduce
   `NOT_CONFIGURED` states) as each subsystem is actually built,
   without changing the `SystemHealth` / `ComponentHealth` contract.
2. `Settings` can grow new typed fields (and `DataMode` can gain real
   meaning beyond `development`) as later phases need them, without
   breaking `load_settings()`'s no-secrets-required guarantee for
   local development.

## Scope of Phase 2

Phase 2 adds the futures-domain layer: what NQ and MNQ are, their
contract economics, their quarterly expiration/roll calendar, and the
regular CME Globex session schedule. Full rationale and known
limitations live in `docs/futures_domain.md`; this section covers how
it fits into the overall architecture. Phase 2 still makes no network
calls, ingests no market prices, and implements no strategies or
predictions.

## Module Map (Phase 2 additions)

```
app/futures/
├── __init__.py    Package marker only. No side effects on import.
├── models.py       Domain types: FuturesInstrument, FuturesContract,
│                   ContractMonth, SettlementType, CycleDateSource,
│                   ContractCycleDates, and the futures error hierarchy.
├── registry.py     FuturesInstrumentRegistry + load_instrument_registry()
│                   (validated NQ/MNQ loading from config/instruments/nq_mnq.json).
├── contracts.py     Contract lifecycle (next_contract, previous_contract,
│                   quarter_contract) and tick/price/dollar economics helpers.
├── calendar.py      nominal_third_friday(), CycleDateCalendar + 
│                   load_cycle_date_calendar() (official vs. calculated-nominal
│                   provenance), final_trading_timestamp().
├── roll.py          calendar_lead_contract(), calendar_lead_contract_at(),
│                   nearest_unexpired_contract(), is_post_roll(), SessionClosedError.
├── sessions.py      SessionState, session_state_at(), trade_date_for(),
│                   ContractLifecycleState, contract_state_at(),
│                   is_contract_within_trading_life().
└── validation.py    (Phase 2.4) Olive PRODUCTION domain integrity:
                    validate_olive_tradable_registry(), validate_olive_cycle_calendar(),
                    validate_olive_futures_domain(), ProductionDomainIntegrityError.
                    Distinct from the generic validation the classes above already
                    perform -- see docs/futures_domain.md §9.

config/
├── instruments/nq_mnq.json                     Stable instrument economics.
└── calendar/cme_equity_index_cycle_dates.json   Source-backed 2025-2028 cycle dates.
```

## Component Relationship (Phase 2)

```
Configuration (config/*.json)
     |
     v
Futures Registry (app.futures.registry)
     |
     +--> Contract Models (app.futures.models)
     |
     +--> Cycle Calendar (app.futures.calendar)
     |
     +--> Roll Resolver (app.futures.roll)
     |
     +--> Session Schedule (app.futures.sessions)
     |
     v
Production Domain Validation (app.futures.validation)   [Phase 2.4]
     |
     v
Health (app.health._check_futures_domain)
```

`app.health._check_futures_domain()` is the only place Phase 2 touches
Phase 1 code: it loads the registry and cycle calendar, validates them
against Olive's required production domain (as of Phase 2.4, via
`app.futures.validation.validate_olive_futures_domain()`), and reports
a real `ComponentHealth` for "Futures domain" (`CONFIGURED` only if
all of that succeeds; `ERROR` with a concise reason otherwise).
`main.py` is unchanged — it already called `get_system_health()`,
which now happens to include a real futures-domain check.

## Architectural Boundaries (Phase 2 additions)

- **`app/futures/models.py`** -- type definitions and validation only.
  No file I/O, no network calls.
- **`app/futures/registry.py`** / **`app/futures/calendar.py`** -- load
  and validate local JSON configuration. No network calls, no market
  data, no provider-specific code (that is Phase 3/4's job).
- **`app/futures/contracts.py`** -- pure contract-lifecycle and
  tick/price arithmetic. No calendar or session logic.
- **`app/futures/roll.py`** -- calendar-based (never liquidity-based)
  lead-contract resolution. Explicitly does not claim to know current
  market liquidity.
- **`app/futures/sessions.py`** -- regular weekly schedule and
  contract-lifecycle (expiration) state only. Not a holiday-aware
  exchange calendar.
- **`app/futures/validation.py`** (Phase 2.4) -- Olive's required
  PRODUCTION domain integrity checks (canonical NQ/MNQ specification,
  required official calendar baseline). Deliberately kept separate
  from the generic validation `models.py`/`registry.py`/`calendar.py`
  already perform -- those stay generic and reusable for any
  well-formed instrument/calendar, not just NQ/MNQ; this module is the
  one place "is this specifically Olive's production domain?" lives,
  and is intended to be reusable by Phase 3+ services as well as
  `app.health`.

Phase 3 (historical market data) is expected to attach provider-
specific symbols/metadata to `FuturesContract` without needing to
rewrite its identity; Phase 4 (real-time data) is expected to enhance
`calendar_lead_contract`'s selection with real volume/open-interest
without changing its calendar-based fallback; Phase 9 (backtesting) is
expected to reuse the cycle-date calendar for realistic historical
contract rolls.

## Scope of Phase 2.1 (corrective hardening)

An external code review of Phase 2 found concrete, reproducible gaps
in domain-model invariant enforcement (see `docs/futures_domain.md`
§2.1 and §3 for the specifics). Phase 2.1 adds **no new architectural
layer or module** -- it strengthens validation inside the existing
Phase 2 module map above:

- `app/futures/models.py` -- `FuturesContract`/`FuturesInstrument`/
  `ContractCycleDates` now validate their own invariants in
  `__post_init__`, regardless of how they are constructed (not only
  via `registry.py`'s JSON loading path).
- `app/futures/contracts.py` -- `next_contract`/`previous_contract`
  now reject cross-instrument navigation and non-quarterly contract
  months with specific domain errors rather than silently misbehaving
  or leaking a bare `ValueError`.
- `app/futures/calendar.py` -- calendar loading now cross-validates
  declared metadata (`as_of`, `covered_years`) against the data it
  actually contains, and rejects non-quarterly months at every public
  entry point.
- `app/health.py` -- the futures-domain health check now verifies the
  tradable registry's roots are *exactly* `{"NQ", "MNQ"}`, not merely
  that the registry loaded without error.

No module boundaries changed, and the Phase 2 module map, component
relationship diagram, and architectural boundaries above remain
accurate as written.

## Scope of Phase 2.2 (second corrective hardening pass)

A second, broader external audit of Phase 2.1 found further
invariant and public-API gaps (see `docs/futures_domain.md` §1, §3,
§4, §6, §7, and §8 for the specifics). Like Phase 2.1, Phase 2.2 adds
**no new architectural layer or module** and preserves every Phase
2.1 fix -- it strengthens the same existing module map further:

- `app/futures/models.py` -- `FuturesInstrument` now rejects
  non-finite `Decimal` economics (`NaN`/`Infinity`/`-Infinity`)
  *before* any comparison that could raise `decimal.InvalidOperation`
  directly; a new centralized `_coerce_point_value` helper backs
  `points_to_ticks`/`points_to_dollars`, rejecting the same non-finite
  values. The pure `nominal_third_friday`/`nominal_customary_roll`
  calendar helpers moved here from `app/futures/calendar.py` (which
  re-exports them unchanged) so that `ContractCycleDates.__post_init__`
  can enforce the exact customary-roll invariant without a circular
  import. Two new error classes, `InvalidDateError` and
  `InvalidDatetimeError`, join the existing hierarchy (`NaiveDatetimeError`
  is now a subclass of `InvalidDatetimeError` rather than a direct
  subclass of `FuturesDomainError`).
- `app/futures/calendar.py` -- `CycleDateCalendar.__init__` now
  performs the full semantic validation itself (metadata, coverage,
  and key/value consistency), so direct construction can no longer
  produce an inconsistent calendar; `load_cycle_date_calendar` parses
  JSON into typed values and delegates to the constructor rather than
  duplicating its checks. The official-dates mapping is stored as a
  `MappingProxyType` for immutability. `get`/`has_official` validate
  `year` as well as `month`, and `final_trading_timestamp` validates
  its input is a `date`.
- `app/futures/registry.py` -- `FuturesInstrumentRegistry.__init__`
  now validates its own mapping (key/value types, key-canonicalization
  agreement, duplicate roots) regardless of construction path; `get`/
  `contains` no longer leak `AttributeError` for a non-string input.
- `app/futures/roll.py` -- `nearest_unexpired_contract`, `is_post_roll`,
  and `calendar_lead_contract` now validate `trade_date` is a plain
  `date` (deliberately rejecting `datetime`, which would otherwise
  silently discard a time-of-day).
- `app/futures/sessions.py` -- the shared `_require_aware_chicago`
  choke point (already used by every public function in this module)
  now validates its input is actually a `datetime` before checking
  for timezone-awareness.

No module boundaries changed, and the Phase 2 / Phase 2.1 module map,
component relationship diagram, and architectural boundaries remain
accurate as written.

## Scope of Phase 2.3 (public API & invariant finalization)

A third external audit of the actual Phase 2.2 deliverable found
further public-API and direct-construction gaps -- not in the domain
rules themselves, but in how defensively the public functions
implementing them handled a caller passing the wrong object type (see
`docs/futures_domain.md` §2, §3, §4, §5, and §6 for the specifics).
Like Phase 2.1 and 2.2, Phase 2.3 adds **no new architectural layer or
module** and preserves every prior fix -- it strengthens the same
existing module map once more, and is explicitly a finalization pass,
not a redesign:

- `app/futures/models.py` -- a new shared validator,
  `_require_plain_date`, centralizes the "must be an exact `date`,
  never a `datetime`" check now used by `ContractCycleDates` and
  `nominal_customary_roll` (previously, a `datetime` either leaked a
  raw cross-type `TypeError` or was returned as a wrong-typed result).
  `FuturesInstrument.__post_init__` now validates `contract_months`'
  *container* type before iterating it (previously, `contract_months=True`
  raised a raw `TypeError: 'bool' object is not iterable`). Construction-time
  economics arithmetic (`tick_size * multiplier`) and all four
  tick/point/dollar conversion methods now catch
  `decimal.DecimalException` around their arithmetic, converting an
  overflow on an individually-finite-but-extreme value into a domain
  error rather than letting a raw `decimal.Overflow` escape. Three new
  shared object validators -- `_require_instrument`, `_require_contract`,
  `_require_cycle_dates` -- and two new error classes --
  `InvalidInstrumentError`, `InvalidContractError` -- back the
  public-API hardening below.
- `app/futures/calendar.py` -- a new shared validator,
  `_require_cycle_calendar` (defined here rather than in `models.py`,
  since `CycleDateCalendar` is defined here and `models.py` has no
  dependency on this module), backs the same public-API hardening.
  `load_cycle_date_calendar`'s `config_path` parameter now accepts any
  path-like value (`Path`/`str`/`os.PathLike`), not only an actual
  `Path`.
- `app/futures/registry.py` -- `load_instrument_registry`'s
  `config_path` parameter gets the identical path-like-acceptance
  treatment. The JSON instrument parser now rejects a non-`int`
  (including a `float` like `3.0`, which Python's `IntEnum` lookup
  treats as equal to the real int `3`) contract-month value before
  attempting the `ContractMonth` conversion.
- `app/futures/contracts.py` -- every public function
  (`quarter_contract`, `next_contract`, `previous_contract`,
  `points_to_ticks`, `ticks_to_points`, `points_to_dollars`,
  `ticks_to_dollars`) now validates its `instrument`/`contract`
  parameters via the new shared validators, rather than leaking a raw
  `AttributeError` for the wrong object type.
- `app/futures/roll.py` -- `nearest_unexpired_contract`,
  `calendar_lead_contract`, and `is_post_roll` now also validate their
  `instrument`/`cycle_calendar`/`cycle` parameters (their `trade_date`
  parameter was already validated as of Phase 2.2); the module's local
  `_require_trade_date` now delegates to `models._require_plain_date`
  instead of duplicating the same check.
- `app/futures/sessions.py` -- `contract_state_at` now validates its
  `contract`/`cycle_calendar` parameters; `is_contract_within_trading_life`
  inherits the fix for free since it delegates straight to
  `contract_state_at`.

No module boundaries changed, and the Phase 2 / Phase 2.1 / Phase 2.2
module map, component relationship diagram, and architectural
boundaries remain accurate as written.

## Scope of Phase 2.4 (production domain integrity finalization)

A fourth external audit of the actual Phase 2.3 deliverable found one
remaining gap -- not in any public function's type-safety (Phase 2.3
closed that), but in what Olive's health check actually verified about
*which* futures domain was loaded (see `docs/futures_domain.md` §9 for
the specifics). The audit demonstrated that a structurally
self-consistent but factually wrong NQ definition, and a cycle-date
calendar silently truncated to only 2025's official rows, both still
reported `Futures domain: CONFIGURED`.

Unlike Phase 2.1-2.3, Phase 2.4 **does** add one new module --
`app/futures/validation.py` -- but it adds no new architectural layer
in the Phase 3+ sense (no provider, no market data, no persistence):
it is a thin, reusable validation layer sitting entirely within the
existing Phase 2 futures-domain package, between the generic domain
objects and `app.health`. It deliberately leaves `FuturesInstrument`,
`FuturesInstrumentRegistry`, and `CycleDateCalendar` exactly as generic
as Phase 2.1/2.2 made them -- the fix is a new, separate layer, not a
change to what those classes already validate:

- `app/futures/validation.py` (new) -- `ProductionDomainIntegrityError`;
  `validate_olive_tradable_registry()` (root-set exactness, now moved
  here from `app.health`, plus exact canonical NQ/MNQ
  financial-specification matching); `validate_olive_cycle_calendar()`
  (required 2025-2028 baseline as a subset check, plus exact
  per-anchor expiration/roll matching against a small immutable
  `OLIVE_REQUIRED_OFFICIAL_CYCLE_DATES` mapping); and
  `validate_olive_futures_domain()`, the single combined entry point.
- `app/health.py` -- `_check_futures_domain()`'s hand-rolled
  tradable-root check is removed (that invariant now has exactly one
  home, in `validation.py`) and replaced with a call to
  `validate_olive_futures_domain()` after the registry and calendar
  load successfully; a validation failure reports `ERROR` with a
  concise reason, exactly like a load failure already did.

No module boundaries changed for `models.py`/`registry.py`/
`calendar.py`/`contracts.py`/`roll.py`/`sessions.py`, and the Phase 2 /
Phase 2.1 / Phase 2.2 / Phase 2.3 module map, component relationship
diagram, and architectural boundaries all remain accurate as written
above (now extended, not replaced, by the one new module and the
updated component-relationship diagram's new `validation.py` step).

## Scope of Phase 2.5 (final public validator hardening)

An internal QA process correction (prompted by a fifth external
audit) found that Phase 2.4's own three new public functions in
`app/futures/validation.py` had not been brought under the parameter-
validation discipline Phase 2.3 established for every other public
function in the futures domain: each trusted its `registry`/`calendar`
argument's type without checking it first, leaking a raw
`AttributeError` for a malformed value, a wrong-kind Olive domain
object, or a hand-built duck-typed stand-in.

Phase 2.5 adds no new module and no new architectural layer -- it is
the narrowest possible fix, confined to the parameter-validation
preamble of three existing functions:

- `app/futures/registry.py` (changed) -- adds `_require_registry()`,
  a shared validator mirroring `app.futures.models._require_instrument`
  / `_require_contract` / `_require_cycle_dates` and
  `app.futures.calendar._require_cycle_calendar` exactly. Also corrects
  two stale docstring references to the pre-Phase-2.4 location of the
  tradable-universe check.
- `app/futures/models.py` (changed) -- adds `InvalidRegistryError`
  (mirroring `InvalidInstrumentError` / `InvalidContractError`) to the
  existing error hierarchy; corrects one stale docstring reference on
  `FuturesInstrument`.
- `app/futures/validation.py` (changed) -- `validate_olive_tradable_registry`
  now calls `_require_registry` first; `validate_olive_cycle_calendar`
  now calls the already-existing `_require_cycle_calendar` first;
  `validate_olive_futures_domain` is unchanged (it already composes the
  two functions above, so it inherits both checks without duplicating
  either).

No change to `app/health.py`'s control flow was needed: it already
only catches `FuturesDomainError`, and the fix's whole purpose is to
make sure that is always what these three functions raise.

With Phase 2.5 delivered, the futures domain's public-API hardening
initiated in Phase 2.1 and extended through Phase 2.4 now covers every
public function in the package, including the production-validation
layer added last. No further Phase 2 corrective work is anticipated
pending the next external review.

## Scope of Phase 3 (historical market data)

Phase 3 adds Olive's first market-data subsystem: the ability to
fetch, validate, and durably store historical OHLCV bars for Olive's
exact NQ/MNQ production universe from a configured provider, on
explicit request only. See `docs/historical_data.md` for the full
write-up (domain model, safety gates, storage layout, Databento
adapter, and this build's known limitations); this section covers how
it fits into Olive's overall architecture.

### Module Map (Phase 3 additions)

```
app/
├── data/
│   ├── __init__.py        Package marker only. No side effects on import.
│   ├── models.py           HistoricalDataError hierarchy, HistoricalTimeframe,
│   │                       DataLabel, HistoricalBarRequest, HistoricalBar,
│   │                       HistoricalFetchStatus/HistoricalFetchResult
│   ├── validation.py       require_olive_tradable_contract (delegates to
│   │                       app.futures.validation), normalize_and_validate_bars
│   ├── provider_base.py    HistoricalMarketDataProvider (ABC), CostEstimate
│   ├── storage.py          HistoricalBarStore (local Parquet, atomic, idempotent)
│   ├── service.py          HistoricalDataService (the gate pipeline)
│   └── providers/
│       ├── __init__.py
│       ├── unconfigured.py   UnconfiguredHistoricalProvider (safe default)
│       ├── databento.py      DatabentoHistoricalProvider (the only module that
│       │                     knows Databento's API shape)
│       └── factory.py        build_historical_provider(settings)
└── health.py (changed)      _check_historical_data() -- real check, replacing
                              the former NOT_IMPLEMENTED placeholder

data/historical/             Local Parquet store root (git-ignored; .gitkeep only)
```

### Component Relationship (Phase 3)

```
app.config.Settings (historical_* fields)
            |
            v
app.data.providers.factory.build_historical_provider()  (never performs I/O)
            |
            v
   HistoricalMarketDataProvider  <--  UnconfiguredHistoricalProvider (default)
            |                    <--  DatabentoHistoricalProvider
            v
app.data.service.HistoricalDataService.fetch_and_store(request)
            |
            |-- 1. request validation (app.data.models)
            |-- 2. Olive production tradable-domain gate
            |        (app.data.validation -> app.futures.validation, reused)
            |-- 3. provider configuration gate
            |-- 4. network opt-in gate
            |-- 5/6. cost estimation + limit gate (provider.estimate_cost)
            |-- 7. provider fetch (provider.fetch_bars)
            |-- 8. cross-bar validation (app.data.validation)
            |-- 9. storage (app.data.storage.HistoricalBarStore)
            v
   HistoricalFetchResult (HistoricalFetchStatus + bars + cost + message)
```

`app.health._check_historical_data()` sits beside this pipeline, not
inside it: it calls the same provider factory to determine
`NOT_CONFIGURED`/`CONFIGURED`/`ERROR`, but never calls
`estimate_cost`/`fetch_bars` on the resulting provider, so checking
system health never performs network I/O or incurs cost.

### Architectural Boundaries (Phase 3 additions)

- **Separate error hierarchy, deliberately composed, never confused:**
  `app.data.models.HistoricalDataError` is a new root, distinct from
  `app.futures.models.FuturesDomainError`. A futures-domain failure
  encountered at this package's boundary is translated into a
  `HistoricalDataError` subclass, never left to leak through as the
  other hierarchy's type (see `docs/historical_data.md` §6 for a defect
  this exact boundary produced, and fixed, during Phase 3 itself).
- **No canonical NQ/MNQ fact is duplicated outside Phase 2:**
  `app.data.validation.require_olive_tradable_contract` delegates
  entirely to `app.futures.validation`'s existing canonical specs.
- **Vendor knowledge stays in one adapter module:** only
  `app.data.providers.databento` knows Databento's API shape (schema
  names, DataFrame layout, exception hierarchy). Every other module in
  `app.data` works only with Olive's own domain objects.
- **No automatic network access:** importing any module in `app.data`,
  loading settings, building a provider via the factory, running the
  test suite, running `main.py`, or computing system health never
  performs network I/O. Only `HistoricalDataService.fetch_and_store`
  can reach a real provider call, and only once every gate in its
  pipeline (§ above) has passed.
- **Fail closed, never fabricate:** a provider failure, a cost-
  estimation failure, or a storage conflict is reported as a distinct
  status/exception, never silently converted into an empty or
  partial success.

With Phase 3 delivered as described here and in
`docs/historical_data.md`, Olive has its first subsystem capable of
building a real local historical dataset for NQ/MNQ -- strictly
on-demand, safety-gated, and fully offline-testable -- while
`Real-time market data` and every later-phase subsystem remain
honestly `NOT_IMPLEMENTED`.

## Scope of Phase 3.1 (historical data safety & integrity correction)

An independent review of the actual delivered Phase 3 ZIP (not merely
its source as summarized above) found several substantive safety/
data-integrity issues -- see `docs/historical_data.md` §16 for the
full, itemized list and the completion report for the complete
accounting. Like Phase 2.1-2.3, Phase 3.1 adds **no new architectural
layer or module boundary** -- every fix lands inside the existing
Phase 3 module map (`app/config.py`, `app/data/models.py`,
`app/data/service.py`, `app/data/providers/databento.py`,
`app/data/providers/factory.py`, `app/data/validation.py`,
`app/data/storage.py`) -- with one partial exception: `Settings`
(`app/config.py`, a Phase 1 module) gains its own `__post_init__`
self-validation for the first time, extending to it the same
"every domain object validates itself regardless of construction
path" discipline every `app.data`/`app.futures` object already
follows, closing the one place in the codebase that had never needed
it until Phase 3 added fields a caller could construct unsafely.

No Phase 3 module boundary, public interface shape, or storage layout
changed in a way that breaks compatibility with anything built against
Phase 3's documented contracts -- every fix narrows what is accepted
(fails closed on a case that previously succeeded unsafely) or adds a
check that was previously skipped (manifest verification on read,
provider-return-type validation), never removes or loosens an existing
guarantee.

## Scope of Phase 3.2 (final historical integrity correction)

A further independent review, of the actual delivered
`olive-phase3.1.zip` (not the source tree in the abstract), found
additional adversarially-discovered gaps -- see
`docs/historical_data.md` §17 for the full, itemized list. Like
Phase 3.1, Phase 3.2 adds **no new architectural layer or module
boundary** -- every fix again lands inside the existing Phase 3 module
map (`app/data/models.py`, `app/data/service.py`,
`app/data/providers/databento.py`, `app/data/storage.py`,
`app/data/validation.py`), plus one new shipped test module
(`tests/test_historical_data_storage_transaction.py`, backed by a new
`tests/_fake_pyarrow.py` fixture) that converts coverage which
previously existed only in an unshipped scratch-space harness into
permanent regression tests. `app/data/validation.py`'s
`normalize_and_validate_bars` gains two new optional keyword
parameters (`instrument`, `expected_provider_name`) rather than a new
function, since the check it performs -- "is this bar correct for the
production instrument/provider actually involved" -- is the same
cross-bar/cross-request validation question that function already
answers, extended by two more cross-checks.

No Phase 3 module boundary, public interface shape, or storage layout
changed in a way that breaks compatibility with anything built against
Phase 3's documented contracts here either -- every Phase 3.2 fix
again either narrows what is accepted (fails closed on a case that
previously succeeded unsafely: a missing `instrument_id`, an
under-covered symbology date range, a production-wrong bar, an
unverified existing partition) or adds a check that was previously
skipped (manifest verification before a write-side merge,
result-object self-validation, rollback-failure surfacing), never
removes or loosens an existing guarantee.

## Scope of Phase 3.3 (canonical historical storage finalization)

A third independent review, of the actual delivered
`olive-phase3.2.zip`, found further adversarially-discovered
canonical-storage integrity gaps -- see `docs/historical_data.md` §18
for the full, itemized list. Like Phase 3.1/3.2, Phase 3.3 adds **no
new architectural layer or module boundary** -- every fix again lands
inside the existing Phase 3 module map (`app/data/models.py`,
`app/data/storage.py`, `app/data/providers/databento.py`), plus one
new shipped, entirely offline real-`databento`-package compatibility
test module (`tests/test_historical_data_databento_real_api.py`,
`pytest.importorskip("databento")`) and substantial new permanent
regression coverage added to the existing
`tests/test_historical_data_storage_transaction.py`. The manifest
format gains two new keys (`partition_year`, `partition_month`) --
additive only, with no migration compatibility required, since there
is no committed/released production dataset predating this build.

No Phase 3 module boundary, public interface shape, or storage layout
changed in a way that breaks compatibility with anything built against
Phase 3's documented contracts here either -- every Phase 3.3 fix
again either narrows what is accepted (fails closed on a case that
previously succeeded unsafely: a pre-existing duplicate's raw count
feeding a result object, a misplaced-month partition, an internally
incoherent partition, a malformed symbology instrument ID, an
out-of-range tick/volume count) or adds a check that was previously
skipped (manifest-content-vs-decoded-bars verification, partition
coherence enforcement, orphan-manifest rejection on read), never
removes or loosens an existing guarantee.

## Scope of the Phase 3.3 QA compliance rework (process correction, still Phase 3.3)

A fourth independent review, of the actual delivered
`olive-phase3.3.zip`, found that the Phase 3.3 completion process had
declared every requirement satisfied without every standing QA
boundary having been exhaustively, adversarially re-tested -- see
`docs/historical_data.md` §19 for the full, itemized list. This is a
correction to the Phase 3.3 delivery and to the completion process,
**not Phase 3.4 and not Phase 4** -- no new architectural layer or
module boundary is added. Every fix again lands inside the existing
Phase 3 module map (`app/data/storage.py`,
`app/data/providers/databento.py`), and the manifest format gains no
new keys -- every existing key is now validated more strictly, never
renamed or restructured. No Phase 3 module boundary, public interface
shape, or storage layout changed in a way that breaks compatibility
with anything built against Phase 3's documented contracts: every fix
narrows what is accepted (a manifest integer field that is a `bool` or
whole-number `float` masquerading as the right value, a `record_count`
of zero, a malformed nested Databento symbology container, an
impossible `HistoricalReadResult`/`HistoricalWriteResult` combination)
or adds a check that was previously missing, never removes or loosens
an existing guarantee.

## Scope of the Phase 3 final completion pass (consolidated close-out, still Phase 3)

A fifth independent review, of the actual delivered Phase 3.3 QA-rework
ZIP, found four remaining gaps one level deeper than anything
previously tested -- see `docs/historical_data.md` §20 for the full,
itemized list. This is the consolidated final correction and
verification pass for Phase 3, **not Phase 3.4 and not Phase 4** -- no
new architectural layer, module, or call site is added. Every fix
again lands inside the existing Phase 3 module map
(`app/data/storage.py`, `app/data/providers/databento.py`); the
manifest format gains no new keys (the already-written
`last_written_at_utc`/`requested_start_utc`/`requested_end_utc` keys
are now validated more strictly, not restructured), and the Databento
adapter gains no new call sites (its existing symbology-response
handling is now validated member-by-member and across sibling entries,
not re-architected). No Phase 3 module boundary, public interface
shape, or storage layout changed in a way that breaks compatibility
with anything built against Phase 3's documented contracts: every fix
narrows what is accepted, never removes or loosens an existing
guarantee. `d0`/`d1` symbology fields were deliberately left
unvalidated after tracing that no code path reads them and that the
properties they would otherwise protect are already independently
guaranteed by existing checks -- a documented decision, not an
oversight. With this pass complete, Phase 3 is considered final and
consolidated; see the `olive-phase3-final.zip` completion report for
the full compliance ledger.

## Scope of Phase 4 (real-time market data)

Phase 4 adds Olive's second market-data subsystem: the ability to open
a live streaming connection to a configured provider for Olive's exact
NQ/MNQ production universe, normalize vendor-specific trade/quote/bar
records into Olive's own domain events, and report connection/
liveness state honestly -- all strictly opt-in, and with no network
access of any kind unless explicitly enabled. See
`docs/realtime_data.md` for the full write-up (domain model, safety
gates, Databento live adapter, reconnect/staleness behavior, and this
build's known limitations); this section covers how it fits into
Olive's overall architecture.

Phase 4 is deliberately a *sibling* to Phase 3, not an extension of
it: live events are never persisted, never written to the historical
Parquet store, and never treated as a substitute for a historical bar.
The two subsystems share only what is genuinely shared already --
Olive's futures domain (`app.futures.*`) and the `DATABENTO_API_KEY`
credential -- and nothing else.

### Module Map (Phase 4 additions)

```
app/
├── data/
│   ├── live_models.py       LiveDataError hierarchy (separate root from
│   │                        HistoricalDataError), ConnectionState,
│   │                        LiveEventType, LiveSubscriptionRequest,
│   │                        LiveTrade/LiveQuote/LiveBar, LiveStreamStatus
│   ├── live_provider_base.py  RealTimeMarketDataProvider (ABC)
│   ├── live_service.py        RealTimeDataService (the gate pipeline +
│   │                           session-aware staleness disambiguation)
│   └── providers/
│       ├── unconfigured_live.py  UnconfiguredLiveProvider (safe default)
│       ├── databento_live.py     DatabentoLiveProvider (the only module
│       │                         that knows Databento's live API shape)
│       └── live_factory.py       build_live_provider(settings)
└── health.py (changed)        _check_realtime_data() -- real check,
                                replacing the former NOT_IMPLEMENTED
                                placeholder, wired in beside (never
                                inside) _check_historical_data()
```

No file under `app/data/` from Phase 3 was renamed, restructured, or
had its public contract changed -- Phase 4 adds new sibling modules
beside the Phase 3 ones (`live_models.py` beside `models.py`,
`live_service.py` beside `service.py`, `databento_live.py` beside
`databento.py`, etc.) rather than growing the Phase 3 modules to cover
both historical and live concerns.

### Component Relationship (Phase 4)

```
app.config.Settings (live_* fields; DATABENTO_API_KEY reused from Phase 3)
            |
            v
app.data.providers.live_factory.build_live_provider()  (never performs I/O)
            |
            v
   RealTimeMarketDataProvider  <--  UnconfiguredLiveProvider (default)
            |                  <--  DatabentoLiveProvider
            v
app.data.live_service.RealTimeDataService.open_stream(subscription)
            |
            |-- 1. subscription validation (app.data.live_models)
            |-- 2. Olive whole-domain production gate
            |        (validate_olive_futures_domain, reused from Phase 2.4)
            |-- 3. per-contract production-tradability gate
            |        (require_olive_tradable_contract, reused from Phase 3)
            |-- 4. provider configuration gate
            |-- 5. network opt-in gate (OLIVE_LIVE_NETWORK_ENABLED)
            |-- 6. provider.connect(subscription, instruments)
            v
   connected RealTimeMarketDataProvider
            |
            |-- .events()  -->  LiveTrade / LiveQuote / LiveBar (DataLabel.LIVE)
            |-- .status    -->  LiveStreamStatus (counts, is_stale)
            |-- .close()   -->  idempotent disconnect
            v
RealTimeDataService.is_feed_unexpectedly_stale()
            |
            v  (reuses app.futures.sessions.session_state_at)
   session-aware staleness verdict (never "stale" during a
   legitimately closed/maintenance session)
```

`app.health._check_realtime_data()` sits beside this pipeline, not
inside it, exactly mirroring `_check_historical_data()`: it calls the
same provider factory to determine
`NOT_CONFIGURED`/`CONFIGURED`/`ERROR`, but never calls
`connect()`/`events()` on the resulting provider, so checking system
health never opens a live connection -- verified directly by a
dedicated test that gives the health check a fake client whose
connection-relevant methods raise `AssertionError` if ever touched.
`main.py` is unchanged -- it already called `get_system_health()`,
which now happens to include a real real-time-data check.

### Architectural Boundaries (Phase 4 additions)

- **Separate error hierarchy, deliberately composed, never confused:**
  `app.data.live_models.LiveDataError` is its own root -- a sibling to
  `app.data.models.HistoricalDataError`, not a subclass of it and not
  reusing it. A futures-domain failure encountered at this package's
  boundary is translated into a `LiveDataError` subclass, the same
  discipline Phase 3 established for `HistoricalDataError`.
- **No canonical NQ/MNQ fact is duplicated outside Phase 2, and no
  tradability rule is duplicated outside Phase 3:**
  `RealTimeDataService.open_stream` reuses
  `app.futures.validation.validate_olive_futures_domain` and
  `app.data.validation.require_olive_tradable_contract` directly,
  rather than re-implementing or re-approximating either check for the
  live path.
- **Vendor knowledge stays in one adapter module:** only
  `app.data.providers.databento_live` knows Databento's live API shape
  (the `Live` client, its record types, its fixed-point price
  encoding, its symbology/error-code conventions). Every other module
  in `app.data`'s live path works only with Olive's own domain
  objects, via duck-typed record classification rather than
  `isinstance` checks against the vendor's own classes -- a
  deliberate choice that lets the exact same code path be exercised
  by tests without the real `databento` package installed, and
  behave identically whether or not it happens to be.
- **No automatic network access:** importing any live-path module,
  loading settings, building a live provider via the factory, running
  the test suite, running `main.py`, or computing system health never
  opens a live connection. Only `RealTimeDataService.open_stream` can
  reach a real provider's `connect()`, and only once every gate in its
  pipeline (§ above) has passed -- including a network kill switch
  (`OLIVE_LIVE_NETWORK_ENABLED`) that is deliberately independent of
  Phase 3's `OLIVE_HISTORICAL_NETWORK_ENABLED`: enabling one never
  enables the other.
- **Fail closed, never fabricate:** an unmapped or contradictorily
  remapped instrument, a tick-misaligned price, a malformed record, or
  a permanent connection failure is reported as a rejection or a raised
  error, never silently dropped or converted into fabricated LIVE
  data. Every live event constructed by the adapter is independently
  validated as `DataLabel.LIVE` in its own `__post_init__`, regardless
  of which code path constructed it.
- **A closed-by-request stream is never reported as "stale":**
  `LiveStreamStatus.is_stale` is computed only while the connection is
  actually `CONNECTED`; a deliberately closed (`STOPPED`) or never-
  connected stream is a different, honestly distinguished state.
- **Staleness is session-aware, not just a clock comparison:**
  `RealTimeDataService.is_feed_unexpectedly_stale()` reuses
  `app.futures.sessions.session_state_at` so a legitimately closed
  weekend or maintenance window is never misreported as an
  unexpectedly stale feed.

With Phase 4 delivered as described here and in
`docs/realtime_data.md`, Olive has its first subsystem capable of
observing live NQ/MNQ market activity -- strictly opt-in, safety-
gated, and fully offline-testable against duck-typed fakes -- while
every later-phase subsystem (feature engineering, strategies,
prediction, signals, backtesting, paper trading, alerting, and the web
layer) remains honestly `NOT_IMPLEMENTED`.
