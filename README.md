# Olive AI

Olive AI is a Hudson Intelligence product being developed as an AI-powered
Nasdaq-100 futures (**NQ** / **MNQ**) market-intelligence and trade-signal
platform. It is designed around depth on one market rather than breadth
across many, and is intended to eventually produce probabilistic
`LONG` / `SHORT` / `NO_TRADE` theses with explicit uncertainty, not
guaranteed outcomes.

## Current Status

**Phase 2.5 — Final Public Validator Hardening** (following a fifth
external code review of Phase 2; intended as the final Phase 2
hardening pass).

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

## Not Yet Implemented

The following are intentionally **not** part of Olive AI yet and will be
added in later phases:

- Historical and real-time market data (prices, quotes, bars)
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
- `.env.example` contains only safe placeholder values.
- No API keys, credentials, or secrets are ever written to source
  files, tests, logs, or documentation.
- Olive AI makes no network calls of any kind through Phase 2 -- the
  futures domain reads only local, version-controlled JSON configuration.

## Development Roadmap

Olive AI is built in 14 sequential phases (Foundation, Futures Domain,
Historical Data, Real-Time Data, Market Intelligence, Strategies,
Prediction Engine, Fusion/Signal Engine, Backtesting, Paper Trading,
Olive Web, Advanced Intelligence, Always-On Operation, and Production
Validation). Each phase is implemented, tested, and committed before
the next begins. See `docs/architecture.md` for architecture details
and `docs/futures_domain.md` for the full Phase 2 futures-domain
write-up.
