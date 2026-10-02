# Olive AI — Architecture (through Phase 2)

This document describes Olive AI's architecture as it exists through
**Phase 2 — Futures Domain & Contract Lifecycle**. The Phase 1 section
below is preserved as originally written; a new Phase 2 section follows
it rather than rewriting that history. It intentionally does not
describe future phases' internals; see the project's phase plan for
what comes next.

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
