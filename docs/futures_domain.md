# Olive AI — Futures Domain (Phase 2)

This document records the Phase 2 decisions for Olive's futures-domain
layer: what NQ and MNQ are, how contract identity and the quarterly
expiration/roll calendar work, how the regular Globex session schedule
is evaluated, and the known limitations of all of the above. A future
developer should be able to understand Phase 2's decisions from this
document without reverse-engineering the test suite.

Phase 2 establishes **domain truth only**. It does not ingest market
prices, does not know about volume or open interest, and makes no
network calls.

## 1. Instrument specifications

Source: CME Group product specifications for E-mini Nasdaq-100 Futures
(NQ) and Micro E-mini Nasdaq-100 Futures (MNQ). As-of date for this
static metadata: **2026-10-01** (see `config/instruments/nq_mnq.json`).

| | NQ | MNQ |
|---|---|---|
| Exchange | CME | CME |
| Underlying | Nasdaq-100 Index | Nasdaq-100 Index |
| Contract multiplier | $20 × index | $2 × index |
| Minimum tick | 0.25 index points | 0.25 index points |
| Tick value | $5.00 | $0.50 |
| Settlement | Cash | Cash |
| Quarterly cycle | Mar / Jun / Sep / Dec | Mar / Jun / Sep / Dec |
| CME month codes | H / M / U / Z | H / M / U / Z |

MNQ is exactly 1/10th NQ's contract multiplier. Tick *size* (0.25
points) is identical between the two products; tick *value* (the
dollar amount one tick is worth) is not — it scales with the
multiplier. `FuturesInstrument` validates this relationship at
construction time (`tick_value == tick_size * multiplier`) rather than
trusting two independently-configured numbers to agree.

All monetary/economic fields (`multiplier`, `tick_size`, `tick_value`)
are `decimal.Decimal`, loaded from JSON **strings** (e.g. `"0.25"`),
never JSON numbers — this avoids floating-point representation issues
entering tick/price/dollar conversions that later phases (backtesting,
paper trading) will depend on.

**Phase 2.2 finite-decimal requirement.** `multiplier`, `tick_size`,
and `tick_value` must always be finite, strictly positive `Decimal`
values. `NaN`, `Infinity`, and `-Infinity` are rejected with
`FuturesConfigurationError` — both on direct construction and when
loaded from JSON (`"Infinity"`/`"NaN"`/`"-Infinity"` are all valid
`Decimal`-constructible strings, so this is checked explicitly via
`Decimal.is_finite()` rather than relying on `Decimal(...)` parsing to
fail). `FuturesInstrument` checks `.is_finite()` *before* any `<=`
comparison on these fields, since comparing a non-finite `Decimal`
directly raises a raw `decimal.InvalidOperation` that must never
reach a caller. The same finite-value requirement applies to the
`points` argument of `points_to_ticks()` / `points_to_dollars()`:
`Decimal`, `int`, `float`, and numeric-string inputs are still
accepted for caller convenience, but `bool`, `None`, non-numeric
strings, and any non-finite value raise `InvalidPointValueError`.

**Phase 2.3 Decimal arithmetic overflow hardening.** "Finite" is a
per-value check, not a guarantee about arithmetic on two finite
values: `Decimal("1E+999999") * Decimal("1E+999999")` is finite on
each operand individually but overflows the active Decimal context's
exponent range, raising a raw `decimal.Overflow` (a subclass of the
broader `decimal.DecimalException`) — a third audit found this at
`FuturesInstrument` construction time (`tick_size * multiplier`) and
in all four conversion methods (`points_to_ticks`, `ticks_to_points`,
`points_to_dollars`, `ticks_to_dollars`) on an individually-finite but
extreme input. The arithmetic in each of these five call sites is now
wrapped in a narrow `try`/`except decimal.DecimalException` (the
common base class, not only `InvalidOperation`, so `Overflow` is also
covered without catching unrelated programming errors) that converts
the failure into the semantically closest existing domain error:
`FuturesConfigurationError` for the construction-time economics
cross-check, `InvalidPointValueError` for the two point-based
conversions, and `InvalidTickCountError` for the two tick-based
conversions. Olive does **not** invent an arbitrary maximum economics
value to "solve" this, and does not silently clamp an extreme value —
it fails closed with a clear, specific domain error instead, exactly
as it does for every other malformed input in this document.

## 2. Contract identity

Internal identity is unambiguous: root symbol + four-digit year +
numeric month (e.g. `NQ-2026-12`). This exists specifically because an
abbreviated single-digit year is ambiguous across decades (2026 and
2036 both end in "6").

`display_code` produces the customary vendor-style code for display
purposes (e.g. `NQZ6`, `MNQH7`) using the CME month letter and the
*last digit* of the year. This is a display format, not a parseable
unambiguous identity — Phase 2 implements **generation** of this code,
not parsing of it, since parsing an abbreviated year without more
context is inherently ambiguous (see master briefing §21).

### 2.1 Construction invariant hardening (Phase 2.1)

An external code review of Phase 2 found that `FuturesContract` and
`FuturesInstrument` could be constructed directly (bypassing
`registry.py`'s JSON-loading validation) with invalid data that then
propagated silently: non-quarterly months, blank/whitespace-only
string fields, non-positive or mismatched economics, and duplicate or
non-quarterly `contract_months`. Phase 2.1 moved this validation into
`__post_init__` on both dataclasses themselves, so it applies no
matter how the object is constructed — not only when it is loaded from
JSON. Root symbols are canonicalized (stripped, upper-cased) at
construction time rather than left for callers to normalize
inconsistently.

Contract navigation (`next_contract` / `previous_contract` in
`app/futures/contracts.py`) now explicitly rejects **cross-instrument**
use: advancing an NQ `FuturesContract` using the MNQ `FuturesInstrument`
(or vice versa) raises `ContractInstrumentMismatchError` rather than
silently producing a contract under the wrong root symbol. A contract
month the instrument does not actually support raises
`InvalidContractMonthError`, never a bare `ValueError` leaked from an
internal `list.index()` lookup.

Tick-count parameters (`ticks_to_points` / `ticks_to_dollars`) now
strictly require a real `int` — a `float`, a fractional `Decimal`, or
a `bool` all raise `InvalidTickCountError` rather than being coerced.
Negative and zero tick counts remain valid (they represent real
economic quantities, e.g. a short position's point/dollar value).

**Phase 2.2 registry-constructor hardening.** `FuturesInstrumentRegistry`
(`app/futures/registry.py`) now validates itself on direct
construction too, not only via `load_instrument_registry()`: every
key must be a non-empty root-symbol string whose canonical
(stripped, upper-cased) form matches its instrument's own
`root_symbol` exactly, every value must actually be a
`FuturesInstrument`, and duplicate roots after canonicalization are
rejected. `get()` and `contains()` never leak a raw `AttributeError`
for a non-string input (e.g. `None`): `get()` raises
`UnknownInstrumentError` and `contains()` simply returns `False`,
since "is this a known instrument?" has an unambiguous "no" for
anything that isn't even a string.

**Phase 2.3 public-API object-type hardening.** A third external
audit found that every public function in `app/futures/contracts.py`
(`quarter_contract`, `next_contract`, `previous_contract`,
`points_to_ticks`, `ticks_to_points`, `points_to_dollars`,
`ticks_to_dollars`) took its `instrument`/`contract` parameters on
faith — passing the wrong object type (e.g. a plain string) leaked a
raw `AttributeError` the first time the function touched one of that
object's attributes, rather than any domain error. Every one of those
functions now validates its domain-object parameters up front via two
shared validators defined in `app/futures/models.py`,
`_require_instrument` and `_require_contract`, raising
`InvalidInstrumentError` / `InvalidContractError` instead. The same
two validators, plus a third (`_require_cycle_dates`, also in
`models.py`) and a fourth defined in `app/futures/calendar.py`
(`_require_cycle_calendar`, which has to live there rather than in
`models.py` since `CycleDateCalendar` itself is defined there and
`models.py` has no dependency on that module), are reused by
`app/futures/roll.py` and `app/futures/sessions.py` — see §5 and §6
below. `FuturesInstrument.contract_months` is also now validated as a
*container* before Olive iterates it at all: previously,
`contract_months=True` (a `bool`, which is not iterable) raised a raw
`TypeError: 'bool' object is not iterable`, and a plain non-bool `int`
had the same problem. Only an actual `list`/`tuple` of individual
month values is now accepted; `str`/`bytes`/`set`/anything else raises
`FuturesConfigurationError`. Finally, a JSON config's contract-month
values must now be true integers: `ContractMonth(3.0) ==
ContractMonth.MARCH` in Python's `IntEnum` lookup, so a JSON float
like `3.0` previously slid through `_parse_instrument` as if it were
the real int `3` — `registry.py` now explicitly rejects any
non-`int`/`bool` raw JSON value before attempting the `ContractMonth`
conversion.

## 3. Official vs. calculated-nominal expiration/roll dates

This is the most important Phase 2 correctness decision.

`nominal_third_friday(year, month)` is a pure function implementing
the normal quarterly-expiration rule ("the third Friday of the
month"). It does **not** account for exchange holidays.

`config/calendar/cme_equity_index_cycle_dates.json` is a small,
explicit, source-backed table of actual CME expiration and customary
roll dates for 2025–2028. Every `ContractCycleDates` returned by
`CycleDateCalendar.get()` carries a `source`:

- `OFFICIAL` — an explicit, source-backed record exists for that
  (year, month) in the table above.
- `CALCULATED_NOMINAL` — no table entry exists; Olive fell back to the
  third-Friday rule (and the customary "Monday of that week" roll
  date) and is labeling the result as non-authoritative.

**The June 2026 exception** demonstrates why this distinction matters:

```
nominal_third_friday(2026, 6)       == 2026-06-19  (a Friday)
cal.get(2026, 6).expiration         == 2026-06-18  (a Thursday, OFFICIAL)
```

An exchange holiday moved the actual CME expiration a day earlier than
the naive third-Friday rule would predict. Code that assumed
"expiration = third Friday" would have been wrong for this contract.
`CycleDateCalendar` always prefers the official table entry when one
exists, so this is handled correctly without special-casing June 2026
anywhere in the roll/session logic.

The table is finite and will go stale: `CycleDateCalendar.as_of` and
`.covered_years` expose exactly how far it currently reaches, and
`has_official(year, month)` lets a caller check before relying on a
result being authoritative. Extending this table (or sourcing it from
a live exchange-calendar feed) is explicitly out of scope for Phase 2.

**Phase 2.1 calendar hardening.** `CycleDateCalendar.get()` and
`.has_official()` only ever accept Olive's four quarterly months (3,
6, 9, 12); a non-quarterly month such as January raises
`CycleDateError` immediately rather than silently falling back to a
calculated date for a contract that can never exist. Every loaded
calendar file's metadata is now cross-validated against its own data:
`as_of` must be a real ISO date (not merely present), and every year
listed in `covered_years` must have a full quarterly set of `OFFICIAL`
entries — a file that claims a year but only supplies one quarter, or
that supplies `OFFICIAL` rows for a year it never lists in
`covered_years`, fails to load. Each individual date row is also
checked for internal consistency: its `expiration`/`roll` dates must
actually belong to the row's own declared `(year, month)`, and `roll`
must fall on a Monday (CME's customary convention) — this check does
**not** require `expiration` to fall on a Friday, which is what
preserves the June 2026 Thursday exception above as valid, official
data rather than rejecting it as malformed.

**Phase 2.2 exact customary-roll invariant.** "Roll falls on a Monday
before expiration" was not a strong enough check — a real Monday that
simply isn't the *correct* customary roll Monday (e.g. September 2026
with `roll=2026-09-07` instead of the actual `2026-09-14`) used to be
accepted. `ContractCycleDates` now requires `roll` to equal exactly
`nominal_customary_roll(nominal_third_friday(year, month))` for its
own declared cycle. Critically, this is anchored to the cycle's
**nominal** third Friday, not its (possibly holiday-shifted)
**official** `expiration` — for June 2026, the official expiration is
Thursday 2026-06-18, but the customary roll (2026-06-15) is still
computed from the nominal Friday (2026-06-19). `nominal_customary_roll()`
itself now only accepts an actual Friday as input and raises
`CycleDateError` otherwise — passing it an official, holiday-shifted
expiration date (such as June 2026's Thursday) is a caller error, not
a valid way to get a roll date; use `nominal_third_friday(year, month)`
first.

**Phase 2.2 direct-construction hardening.** `CycleDateCalendar` can
no longer reach an inconsistent state regardless of how it is built.
Its constructor — not `load_cycle_date_calendar()` — now performs all
of the semantic validation: `official_dates` must be a mapping whose
`(year, month)` keys agree with their values' own declared
year/month and whose values are all `OFFICIAL`-sourced
`ContractCycleDates`; `covered_years` must be a non-empty sequence of
in-range, non-bool, duplicate-free integers; `as_of` must be a real
ISO date; `source` must be a non-empty string; and the
quarterly-coverage cross-check (every claimed year has all four
quarters; every official year is claimed) still applies. The loader
parses raw JSON into these typed values and then simply calls the
constructor, wrapping any resulting error with the file path for
context — the validation logic itself lives in exactly one place.
`CycleDateCalendar` also stores its official-dates mapping as a
`types.MappingProxyType`, so a caller's continued reference to the
dict they passed in (or to the calendar's own internal mapping)
cannot mutate the calendar's truth after construction.

**Phase 2.2 year/month input validation.** `nominal_third_friday`,
`CycleDateCalendar.get()`, and `CycleDateCalendar.has_official()` all
validate `year` (rejecting `bool`, non-`int`, and out-of-range values)
before doing any date arithmetic, so a malformed year such as `True`
or `"2026"` raises a domain error rather than a raw `TypeError` from
the stdlib `calendar` module.

**Phase 2.3 date-vs-datetime invariant.** `ContractCycleDates` and
`nominal_customary_roll()` previously accepted a `datetime` wherever a
plain `date` was expected — because `datetime` subclasses `date`, a
naive `isinstance(value, date)` check lets one through silently. For
`nominal_customary_roll()`, that meant a `datetime` argument was
returned as a wrong-typed `datetime` result rather than being
rejected. For `ContractCycleDates`, a `datetime` `expiration`/`roll`
field previously leaked a raw `TypeError` the moment it was compared
against another plain `date` internally. Both now route through a
single shared validator, `app.futures.models._require_plain_date`,
which requires `type(value) is date` exactly — not merely
`isinstance(value, date)` — so a `datetime` is deliberately rejected
with `InvalidDateError` rather than silently truncated or compared
cross-type. This is the same validator `app.futures.roll`'s
`trade_date` parameters already used as of Phase 2.2 (that module's
local `_require_trade_date` now simply delegates to it, so the
checking logic lives in exactly one place).

**Phase 2.3 config-loader path hardening.** `load_instrument_registry`
and `load_cycle_date_calendar` previously required their `config_path`
argument to be an actual `pathlib.Path` — a plain `str` (a very
natural thing to pass) leaked a raw `AttributeError` the first time
`.exists()` was called on it. Both loaders now accept anything
path-like (`Path`, `str`, or any other `os.PathLike`), normalizing it
to a `Path` via `Path(config_path)` inside a `try`/`except TypeError`
that raises the module's own domain error
(`FuturesConfigurationError` / `CycleDateError`) for a value that
isn't path-like at all (e.g. an `int`). `config_path=None` still means
"use the real config file" in both loaders.

## 4. Final trading timestamp

Quarterly NQ/MNQ contracts terminate at **8:30 a.m. America/Chicago**
on their official expiration date. `final_trading_timestamp()` always
returns a timezone-aware `datetime` — Olive never constructs a naive
"final trading time."

`final_trading_timestamp()` requires its input to be a `date` (a
`datetime` is also accepted, since it subclasses `date`, but only its
year/month/day are used — any time-of-day on the input is
intentionally ignored, since the result's time is always the fixed
8:30 a.m. termination time). A non-date input raises `InvalidDateError`
rather than a raw `AttributeError`.

## 5. Contract lifecycle (expiration) state

`contract_state_at(contract, timestamp, cycle_calendar)` returns
`TRADING` for any timestamp strictly before the contract's final
trading timestamp, and `EXPIRED` at and after it. This is independent
of the general Globex session state — a contract can be `EXPIRED` even
during an otherwise `OPEN` session (because a *different*, later
contract keeps trading).

Phase 2 only distinguishes `TRADING` / `EXPIRED`. A third `FUTURE`
state (not yet listed) is not produced, because Olive does not yet
track contract listing dates — there is nothing meaningful to compute
it from yet. This is a deliberate Phase 2 scope boundary, not an
oversight.

**Phase 2.3 public-API object-type hardening.** `contract_state_at`
previously took its `contract`/`cycle_calendar` parameters on faith —
a wrong-typed `contract` (e.g. a plain string) leaked a raw
`AttributeError` from `contract.year`, and a wrong-typed
`cycle_calendar` leaked one from `cycle_calendar.get(...)`. Both are
now validated up front (`InvalidContractError` /
`app.futures.calendar.CycleDateError` respectively) via the same
shared validators described in §2 above. `is_contract_within_trading_life`
delegates directly to `contract_state_at`, so it inherits this
validation automatically with no separate fix needed.

## 6. Calendar lead vs. actual liquidity leader

**Olive has no market data in Phase 2.** It cannot know which contract
is actually most liquid right now. Everything this layer computes is
named accordingly: `calendar_lead_contract` / `calendar_lead_contract_at`,
never `actual_active_contract`.

The algorithm implements CME's customary U.S. equity-index roll
convention:

1. Find the **nearest unexpired** quarterly contract (`nearest_unexpired_contract`):
   the soonest contract whose expiration date has not yet passed.
2. If the current trade date is **before** that contract's customary
   roll date, the calendar lead *is* that nearest contract.
3. If the trade date is **on or after** the roll date, the calendar
   lead becomes the *next* quarterly contract out — even though the
   nearer contract is still technically tradable until its own
   expiration a few days later.

`nearest_unexpired_contract()`, `is_post_roll()`, and
`calendar_lead_contract()` all require `trade_date` to be a plain
`date` — a `datetime` is deliberately **rejected**, even though it
subclasses `date`, rather than silently discarding its time-of-day:
callers with a timestamp should derive an explicit trade date via
`app.futures.sessions.trade_date_for()` first. A non-date input (a
string, `None`, a `datetime`) raises `InvalidDateError` instead of a
raw `AttributeError`.

That `trade_date` coverage was real as of Phase 2.2, but, as a third
audit found, narrower than it sounded: these same three functions'
*other* parameters — `instrument` and `cycle_calendar` on
`nearest_unexpired_contract()`/`calendar_lead_contract()`, and `cycle`
on `is_post_roll()` — were still taken on faith and could still leak a
raw `AttributeError`. **Phase 2.3 public-API object-type hardening**
closes that: all three now also validate those parameters via the
shared validators from `app.futures.models`
(`_require_instrument`, `_require_cycle_dates`) and
`app.futures.calendar` (`_require_cycle_calendar`), raising
`InvalidInstrumentError` / `CycleDateError` instead.
`calendar_lead_contract_at()` inherits the same validation for free,
since it delegates straight to `calendar_lead_contract()` after
resolving its timestamp to a trade date.

Verified against the source-backed September 2026 roll date (2026-09-14):

```
2026-09-13 (before roll) -> calendar lead = NQU6
2026-09-14 (at roll)     -> calendar lead = NQZ6
2026-09-15 (after roll)  -> calendar lead = NQZ6
```

And the December 2026 → March 2027 year-end transition (roll
2026-12-14):

```
2026-12-13 -> calendar lead = NQZ6
2026-12-14 -> calendar lead = NQH7   (year rolls 2026 -> 2027)
```

MNQ mirrors NQ's roll behavior exactly (`MNQU6 -> MNQZ6`, `MNQZ6 -> MNQH7`)
since both trade the same quarterly cycle off the same calendar.

A later phase (real-time market data) may use actual volume/open
interest to enhance or override this calendar-based selection — that
enhancement does not exist yet and must not be implied by Phase 2's
terminology or documentation.

## 7. Regular Globex session schedule

`session_state_at(timestamp)` evaluates the **regular weekly**
schedule only, in `America/Chicago` (via `zoneinfo.ZoneInfo`, never a
hard-coded UTC offset, so DST transitions are handled correctly):

```
Sunday:            < 17:00 CT  WEEKEND_CLOSED   >= 17:00 CT  OPEN
Monday - Thursday: [16:00, 17:00) CT  MAINTENANCE   otherwise  OPEN
Friday:            < 16:00 CT  OPEN              >= 16:00 CT  WEEKEND_CLOSED
Saturday:          WEEKEND_CLOSED (always)
```

**Important truthfulness note:** this is the *regular weekly schedule*,
not a complete, authoritative CME trading calendar. It does not know
about exchange holidays, emergency closures, or special schedules.
`SessionState.OPEN` means "the regular weekly schedule says open," not
"CME is definitely open today." A later phase may introduce an
authoritative, holiday-aware exchange calendar; Phase 2 does not
fabricate one.

All inputs must be timezone-aware `datetime` objects. A naive datetime
raises `NaiveDatetimeError` rather than being silently assumed to mean
Central Time. A value that is not a `datetime` at all (e.g. a string
or `None`) raises the broader `InvalidDatetimeError` instead of a raw
`AttributeError` — `NaiveDatetimeError` is a subclass of it, so
catching `InvalidDatetimeError` covers both cases.

### Trade date mapping

`trade_date_for(timestamp)` maps a session timestamp to its futures
trade date:

- During an `OPEN` session before 17:00 CT, the trade date is the
  calendar date itself.
- From 17:00 CT onward, the trade date rolls forward to the next
  calendar day (the evening session "belongs" to the next trade date).
- During `MAINTENANCE` or `WEEKEND_CLOSED`, there is no single
  unambiguous trade date, so `trade_date` is `None` — Olive returns an
  explicit "no trade date" result (`TradeDateResult`) rather than
  guessing one.

Examples: Sunday 18:00 CT → Monday trade date; Monday 10:00 CT →
Monday trade date; Monday 18:00 CT → Tuesday trade date.

The roll resolver's `calendar_lead_contract_at(..., timestamp, ...)`
convenience wrapper uses this trade-date mapping (not a naive calendar
comparison) and raises `SessionClosedError` if called during
maintenance or a weekend closure, since no unambiguous trade date
exists to roll against in that window.

## 8. Health system integration

`app.health._check_futures_domain()` performs a real check: it loads
the instrument registry, loads the cycle-date calendar, and (as of
Phase 2.4 — see §9 below) validates that both actually satisfy Olive's
required PRODUCTION NQ/MNQ domain, via
`app.futures.validation.validate_olive_futures_domain()`. Only if all
of that succeeds does `Futures domain` report `CONFIGURED`; any
failure reports `ERROR` with a concise reason. This is not a
hard-coded status string.

**Phase 2.1 note:** `FuturesInstrumentRegistry` / `load_instrument_registry`
(`app/futures/registry.py`) are a deliberately *generic* loader — they
will accept any well-formed instrument configuration, not just
NQ/MNQ, which is what makes the malformed-configuration tests
possible without touching the real config file. The production
"Olive only trades NQ/MNQ" constraint is enforced separately (as of
Phase 2.4, in `app.futures.validation`, not directly in the health
check — see §9), against the real, loaded
`config/instruments/nq_mnq.json`. A config that loads successfully but
contains an unexpected extra root (e.g. `"ES"`) is still rejected as
`ERROR`, even though the generic registry loader would have accepted
it.

## 9. Olive production-domain integrity validation (Phase 2.4)

**Generic structural validity is not the same question as "is this
actually Olive's NQ/MNQ?"** A fourth external audit demonstrated this
concretely: it modified the production NQ configuration to
`multiplier=2, tick_size=0.25, tick_value=0.50, contract_months=[3]`.
Because `0.25 × 2 == 0.50`, `FuturesInstrument`'s own generic
validation (§2.1) correctly considered this internally consistent —
and the registry still contained exactly `{"NQ", "MNQ"}` — so, before
Phase 2.4, the health check reported `Futures domain: CONFIGURED` for
a production instrument that was not actually NQ at all. The same
audit truncated `config/calendar/cme_equity_index_cycle_dates.json`
down to a complete, self-consistent set of 2025-only entries; `
CycleDateCalendar`'s own cross-validation (§3) only requires that
declared coverage matches actual data, not any particular *amount* of
coverage, so this also loaded successfully and also reported
`CONFIGURED` — despite Olive having silently lost its required
2026-2028 source-backed baseline, including the June 2026 holiday
exception (§3).

`app/futures/validation.py` exists to close exactly this gap, as a
distinct third layer:

1. **Generic structural validity** (`app.futures.models`,
   `app.futures.registry`, `app.futures.calendar`) — "Is this a
   well-formed, internally self-consistent instrument / registry /
   calendar?" Deliberately has no opinion about *which* product a root
   symbol describes.
2. **Olive production-domain integrity** (`app.futures.validation`,
   new in Phase 2.4) — "Is this *specifically* the real NQ/MNQ product
   Olive is designed to trade, with the source-backed calendar
   coverage Phase 2 shipped with?"
3. **Health reporting** (`app.health`) — calls layer 2 (which itself
   depends on layer 1 having already succeeded) and translates the
   result into `CONFIGURED` / `ERROR`.

This module does not reimplement any domain arithmetic or structural
validation layer 1 already performs (tick-value cross-checks,
quarter-month coercion, generic mapping/key validation). It answers a
narrower, later question, and is deliberately reusable — by
`app.health`, and by any future Phase 3+ service that needs to
establish the domain foundation is trustworthy before trusting it with
real money.

### 9.1 Canonical NQ/MNQ production specification

`validate_olive_tradable_registry(registry)` checks two independent
things: that the registry's roots are *exactly* `{"NQ", "MNQ"}`
(absorbed here from what used to be a hand-rolled check inside
`app.health` — that invariant now has exactly one home), and that each
of the NQ and MNQ entries matches Olive's canonical Phase 2
financial/economic specification *exactly*, via `Decimal` value
comparison:

| | NQ | MNQ |
|---|---|---|
| Exchange | `CME` | `CME` |
| Underlying | `Nasdaq-100 Index` | `Nasdaq-100 Index` |
| Currency | `USD` | `USD` |
| Multiplier | `20` | `2` |
| Tick size | `0.25` | `0.25` |
| Tick value | `5.00` | `0.50` |
| Settlement | `CASH` | `CASH` |
| Quarterly months | `{3, 6, 9, 12}` exactly | `{3, 6, 9, 12}` exactly |

A production NQ/MNQ entry missing any one of the four required
quarterly months, or (structurally impossible today, since Olive's
quarterly-month universe is exactly `{3, 6, 9, 12}`, but checked
defensively regardless) carrying an unexpected extra one, fails
validation. Every mismatch across every field is collected into one
error message rather than stopping at the first, since a real
corruption (as in the external audit) often changes several fields
together.

**Design decision on `display_name`:** the configured display strings
("E-mini Nasdaq-100 Futures", "Micro E-mini Nasdaq-100 Futures") are
accurate, but they are presentation metadata, not part of the
financial contract. A harmless capitalization or wording change must
never, by itself, make an otherwise-correct NQ/MNQ definition report
as a broken production domain. `display_name` is intentionally **not**
part of the canonical spec checked above; the financial-identity
fields in the table are what Olive's trade economics and contract
identity actually depend on.

### 9.2 Official calendar baseline protection

`validate_olive_cycle_calendar(calendar)` also checks two independent
things:

1. `calendar.covered_years` must be a **superset** of Olive's required
   baseline years, `{2025, 2026, 2027, 2028}` — never required to
   equal that set exactly, so a legitimate future update that adds
   2029, 2030, ... remains valid without any change to this module. A
   calendar's covered years must *include* the baseline; it may always
   include more.
2. Every row in `OLIVE_REQUIRED_OFFICIAL_CYCLE_DATES` — a small,
   explicit, immutable mapping of all sixteen 2025-2028 quarterly
   `(year, month) -> (expiration, roll)` pairs, matching
   `config/calendar/cme_equity_index_cycle_dates.json` exactly — must
   still be present, still `OFFICIAL` (not silently fallen back to
   `CALCULATED_NOMINAL`), and still carry its exact required dates.
   This is what catches an individual anchor (most notably the June
   2026 holiday exception) being corrupted to a different,
   still-structurally-valid date while the calendar otherwise still
   claims full coverage.

**Engineering choice (full-snapshot validation):** rather than
checking only the June 2026 anchor plus year-level coverage, this
module validates the complete 2025-2028 snapshot row by row. The data
is small, stable, and already exists in the real config file, so
validating all of it costs nothing extra and catches corruption to
*any* source-backed row, not only the headline one. The mapping lives
in exactly one place (`app.futures.validation`), not scattered across
modules.

A calendar missing part of this baseline, or with the real NQ/MNQ
instruments corrupted, now reports `Futures domain: ERROR` instead of
`CONFIGURED` — closing the exact gap the fourth audit found, while the
real, unmodified `config/instruments/nq_mnq.json` and
`config/calendar/cme_equity_index_cycle_dates.json` continue to pass
every check and report `CONFIGURED`.

### 9.3 What this validation can and cannot guarantee

This module validates Olive's *configured* domain against a known-good
snapshot captured at Phase 2's `as_of` date (2026-10-01). It cannot by
itself detect a *future* CME rule change that hasn't yet been reflected
in `config/calendar/cme_equity_index_cycle_dates.json` — if CME were to
change a 2027 roll convention tomorrow, this validator would continue
to (correctly, given its inputs) approve the existing 2027 row until
someone updates the config and, if appropriate, this module's required
mapping. It guards against *configuration drift and corruption*, not
against the exchange itself changing its rules out from under a static
file.

### 9.4 Public-parameter hardening (Phase 2.5)

An internal QA process correction (triggered by a fifth external
audit) found that `validate_olive_tradable_registry`,
`validate_olive_cycle_calendar`, and `validate_olive_futures_domain` —
despite §9's framing of these as reusable public entry points —
validated the *contents* of their `registry`/`calendar` arguments
without ever validating the arguments' *types* first. A caller passing
`None`, a primitive, or even a different Olive domain object (a
`CycleDateCalendar` where a registry was expected, or vice versa) got
a raw `AttributeError` instead of a domain error — the same failure
category Phase 2.3 established must never happen at a public
futures-domain boundary, present again in the newest module in the
package.

Each function now validates its parameters first, via the shared
`_require_registry` (`app.futures.registry`) and the already-existing
`_require_cycle_calendar` (`app.futures.calendar`) validators — the
same pattern already used throughout `app.futures.contracts` /
`app.futures.roll` / `app.futures.sessions`, not new duplicate logic.
This closes the gap for primitives, for the cross-domain-object-swap
case, and for a hand-built duck-typed stand-in that merely exposes the
right attribute names: the fix is a genuine `isinstance` check against
the real class, so only an object that has already passed that class's
own internal self-consistency validation at construction can proceed.

## 10. Known limitations (Phase 2)

- No live volume/open-interest-based active-contract selection yet —
  `calendar_lead_contract` is a calendar convention, not evidence of
  current liquidity.
- No historical or live market-data provider integration (Phases 3/4).
- The regular session schedule is not a complete, dynamic CME holiday
  service — only the weekly pattern is modeled.
- The source-backed cycle-date table covers 2025–2028 only; dates
  outside that range fall back to a labeled `CALCULATED_NOMINAL`
  calculation.
- Olive's production-domain validation (§9) checks configuration
  against a known-good snapshot; it cannot detect a future CME rule
  change that hasn't yet been reflected in the config file and, if
  appropriate, `OLIVE_REQUIRED_OFFICIAL_CYCLE_DATES` (see §9.3).
- No market prices, no strategies, no predictions — Phase 2 is domain
  knowledge only.
