# Olive AI — Historical Market Data (Phase 3, final as of the Phase 3 final completion pass)

This document describes Olive's historical market-data subsystem as it
stands today: what it does, what it deliberately does not do, its
safety model, its storage layout, and its known limitations. It
assumes familiarity with `docs/architecture.md` and
`docs/futures_domain.md` (Phase 1-2).

Phase 3 went through five corrective passes, each triggered by a fresh
independent review of the previously delivered build: **Phase 3.1**
(§16), **Phase 3.2** (§17), **Phase 3.3** (§18), the **Phase 3.3 QA
compliance rework** (§19), and the **Phase 3 final completion pass**
(§20, the current state). The detailed per-pass history in §16-§20 is
kept for anyone who wants it; this document's other sections describe
only the current, final system — not a chronological account of every
bug ever found along the way.

## 1. Scope

Phase 3 adds the ability to fetch, validate, and durably store
historical OHLCV bars for Olive's exact production tradable universe
(`NQ`, `MNQ`) from a configured provider, on explicit request. It adds:

- A provider-independent historical-data interface
  (`app/data/provider_base.py`) and two implementations: a safe default
  (`UnconfiguredHistoricalProvider`) and a real Databento adapter
  (`DatabentoHistoricalProvider`).
- Typed, self-validating request/bar/result domain objects
  (`app/data/models.py`).
- Olive-production-tradability and cross-bar validation
  (`app/data/validation.py`), reusing Phase 2's existing production
  validation layer rather than duplicating any NQ/MNQ fact.
- A local, deterministic, atomic Parquet storage layer
  (`app/data/storage.py`).
- An orchestration service (`app/data/service.py`,
  `HistoricalDataService`) that gates every fetch through Olive's
  production-domain check, request validation, provider configuration,
  an explicit network opt-in, and a cost limit -- in that order --
  before any provider call, and turns every outcome into an explicit
  `HistoricalFetchResult`/`HistoricalFetchStatus`.
- A real `Historical market data` health check (`app/health.py`).

## 2. Explicitly out of scope (deferred to later phases)

Phase 3 does **not** implement, and this build contains no code path
for: real-time/live streaming data; MBO/MBP-10/MBP-1 or any order-book
reconstruction; back-adjusted/continuous price series; feature
engineering (moving averages, RSI, or any derived indicator);
backtesting (fills, slippage, PnL); any tradable scope beyond NQ/MNQ;
or any UI. `Real-time market data` continues to report
`NOT_IMPLEMENTED` in the health report -- Phase 3 touches nothing about
it.

## 3. Exact contract / symbology policy

Olive always requests one exact futures contract using Databento's
`stype_in="raw_symbol"` with the contract's own CME-style display code
(`FuturesContract.display_code`, e.g. `NQZ6`, `MNQZ6`) -- derived
internally from Olive's Phase 2 `FuturesContract`, never accepted as a
raw string from a caller. Olive never uses Databento's **parent**
symbology (`NQ.FUT`, which mixes outright futures and spreads) or
**continuous** symbology (`NQ.c.0`/`.v.0`/`.n.0`, which encodes
Databento's own roll rules, not Olive's) for canonical storage. A
provider's own `instrument_id` is recorded only as optional provenance
(`HistoricalBar.provider_instrument_id`) -- Olive's persistent contract
identity is always `FuturesContract.identity` (e.g. `NQ-2026-12`).

`DatabentoHistoricalProvider.fetch_bars` additionally checks that every
row the provider returns reports the same `symbol` Olive requested,
rejecting the response (`ProviderResponseIdentityError`) if it does
not. Olive never trusts provider response identity without checking it.

**Phase 3.1 addition — raw-symbol decade-reuse protection.** Databento
documents that a raw symbol's trailing digit is only the *last digit*
of the contract year, so `NQZ6` identifies a December contract in
**2016, 2026, and 2036 alike**. Checking that a response row's own
`symbol` equals the requested raw symbol (the check above) is
therefore **not sufficient** on its own — a stale cache, a
misconfigured date range, or a request built from the wrong year could
honestly receive rows that self-report `symbol="NQZ6"` while actually
belonging to a different contract-year's instrument, silently
corrupting Olive's historical record under the wrong `contract_identity`.
Before any paid `timeseries.get_range` call,
`DatabentoHistoricalProvider._resolve_instrument_id` uses Databento's
**free** `symbology.resolve` endpoint to resolve the raw symbol to its
instrument ID (a) within the requested contract's own calendar month
and (b) over the request's actual date range, and requires both to
agree — a resolution that is missing, ambiguous (resolves to more than
one distinct instrument ID), partial, or disagreeing between the two
windows raises `ProviderSymbologyError` and the paid fetch never
happens. Every returned row's own `instrument_id` is then cross-checked
against that resolved value too, as defense in depth.

## 4. Supported timeframes

`HistoricalTimeframe`: `ONE_SECOND`, `ONE_MINUTE`, `ONE_HOUR`, `ONE_DAY`,
mapped explicitly to Databento's OHLCV schemas `ohlcv-1s` / `ohlcv-1m` /
`ohlcv-1h` / `ohlcv-1d` via `HistoricalTimeframe.databento_schema`. No
other Databento schema (MBO, MBP-1, MBP-10, definition, statistics,
...) is used anywhere in this codebase.

## 5. Domain model

- `HistoricalBarRequest` (immutable): `contract` (a real
  `FuturesContract`), `timeframe`, `start`/`end` (both normalized to
  timezone-aware UTC; a naive datetime or a plain `date` is rejected,
  never silently misinterpreted). `start` is inclusive, `end` is
  exclusive, and `start < end` strictly. Does **not** itself check that
  `contract` is one of Olive's production tradable roots -- see §7.
- `HistoricalBar` (immutable): Olive's canonical stored bar. Prices are
  signed integer tick counts (`open_ticks`/`high_ticks`/`low_ticks`/`close_ticks`)
  plus a `tick_size: Decimal`, never `float` or a bare `Decimal` price
  -- `open`/`high`/`low`/`close` properties compute the exact `Decimal`
  price as `ticks * tick_size`. `data_label` must be exactly
  `DataLabel.HISTORICAL`; there is no code path in Phase 3 capable of
  constructing a bar labeled `LIVE`/`DELAYED`/`SIMULATED`/`DEMO`.
  `HistoricalBar.from_decimal_prices(...)` is the one sanctioned way to
  build a bar from a provider's Decimal prices: it **rejects, never
  rounds,** a price that is not an exact integer multiple of
  `tick_size` (`HistoricalPriceTickMisalignedError`).
- `HistoricalFetchStatus` / `HistoricalFetchResult`: every outcome of
  `HistoricalDataService.fetch_and_store` is one of `SUCCESS`,
  `SUCCESS_EMPTY`, `NOT_CONFIGURED`, `REJECTED_INVALID_REQUEST`,
  `REJECTED_NOT_TRADABLE`, `REJECTED_NETWORK_DISABLED`,
  `REJECTED_COST_LIMIT`, `REJECTED_COST_ESTIMATE_FAILED`, or `FAILED` --
  never `None`, an empty list, or a bare exception that a caller must
  interpret.

## 6. Error hierarchy

`HistoricalDataError` (new, separate root from
`app.futures.models.FuturesDomainError` -- a new subsystem, not an
extension of the futures domain) and its subclasses
(`InvalidHistoricalRequestError`, `InvalidHistoricalBarError`,
`HistoricalPriceTickMisalignedError`, `NotOliveTradableContractError`,
`HistoricalBarContractMismatchError`, `HistoricalBarOutOfRangeError`,
`HistoricalBarConflictError`, `HistoricalStorageError` and its
`HistoricalStorageIntegrityError` subclass,
`InvalidHistoricalServiceConfigurationError` (Phase 3.1: malformed
constructor arguments to `HistoricalDataService`/
`build_historical_provider`), and `HistoricalProviderError` and its
subclasses, including two added in Phase 3.1: `ProviderDataError`
(a provider row-data-quality failure, e.g. a tick-misaligned price or
a fractional volume) and `ProviderSymbologyError` (an identity-safety-
gate failure from the raw-symbol decade-reuse protection above). Two
more subclasses of `HistoricalBarContractMismatchError` were added in
Phase 3.2: `HistoricalBarProductionEconomicsMismatchError` (a bar's
`tick_size` disagrees with Olive's production-validated instrument —
§7/§8) and `HistoricalBarProviderProvenanceMismatchError` (a bar's own
`provider`/`provider_raw_symbol` disagrees with the provider actually
servicing the request, or the exact raw symbol Olive derived — §8). A
futures-domain
violation encountered at this package's boundary (e.g. a caller passing
something that isn't a real `FuturesContract`) is translated into one
of these types, never left as a raw `app.futures.models.FuturesDomainError`
leaking into a caller that only expects to catch this hierarchy.

> **Found and fixed during Phase 3's own adversarial test-writing
> pass:** `HistoricalBarRequest.__post_init__`,
> `HistoricalBar.from_decimal_prices`, and
> `require_olive_tradable_contract` initially called the futures
> domain's own `_require_contract`/`_require_registry` directly,
> without translating a resulting `FuturesDomainError` into this
> package's own error type -- exactly the boundary-leak defect class
> Phase 2.4/2.5 were built to close, recurring in this brand-new
> subsystem. All three call sites now translate explicitly, with
> regression tests in `tests/test_historical_data_models.py` and
> `tests/test_historical_data_validation.py`.

## 7. Generic structural validity vs. Olive production correctness

Continuing the distinction established in Phase 2
(`docs/futures_domain.md` §9): `HistoricalBarRequest`/`HistoricalBar`
only check that they are internally well-formed -- a bar for a
well-formed but invented root like `"ZZ"` is structurally valid by
their own standard. Whether a contract is actually one of Olive's
required **production** NQ/MNQ instruments is answered separately by
`app.data.validation.require_olive_tradable_contract`, which delegates
entirely to Phase 2's existing `app.futures.validation` layer (its
`REQUIRED_TRADABLE_ROOTS`, `_CANONICAL_SPECS`, and
`_validate_instrument_matches_spec`) -- no NQ/MNQ fact is duplicated in
the data layer. `HistoricalDataService` calls this gate **before any
provider interaction of any kind** (see §9) -- a request for `ES` or
any other non-Olive root never reaches cost estimation or fetching,
proven in `tests/test_historical_data_service.py` with a provider whose
methods assert if ever called.

## 8. Cross-bar validation (`normalize_and_validate_bars`)

A different question from the above: given a batch of already-
structurally-valid bars, are they consistent *with each other* and with
the request that produced them? This function (deliberately **not**
over-validated against Phase 2's incomplete regular-session model --
provider timestamps are treated as canonical evidence, not
cross-checked against Olive's session calendar) checks: every bar's
contract identity and timeframe match the request exactly
(`HistoricalBarContractMismatchError`); every bar's `ts_event` falls
inside the request's half-open `[start, end)` window
(`HistoricalBarOutOfRangeError`) -- rejecting any future leakage or
out-of-window data a provider might return; bars are deterministically
sorted by `ts_event` ascending regardless of the order the provider
returned them; and bars sharing a canonical key (contract + timeframe +
`ts_event`) with identical content are de-duplicated to one record,
while bars sharing a key with **conflicting** content raise
`HistoricalBarConflictError` -- Olive never silently picks a winner.

**Phase 3.2 addition — production economics / provider-provenance
cross-checks (§10-12 of the Phase 3.2 correction).** A bar can be
individually self-consistent (its own `__post_init__` already
guarantees `high_ticks * tick_size` etc. form sensible prices) and
pass every cross-bar check above, while still being **production-wrong**
— the historical-data instance of the Phase 2 "generic structural
validity vs. Olive production correctness" distinction (§7). When
`HistoricalDataService` calls `normalize_and_validate_bars`, it now
also passes `instrument` (the already production-validated
`FuturesInstrument` from `require_olive_tradable_contract`) and
`expected_provider_name` (the provider actually servicing this
request). Every bar's `tick_size` must exactly equal
`instrument.tick_size`
(`HistoricalBarProductionEconomicsMismatchError` otherwise); every
bar's `provider` must equal `expected_provider_name` and its
`provider_raw_symbol` must equal `request.contract.display_code`
(`HistoricalBarProviderProvenanceMismatchError` otherwise). This is
defense in depth against a buggy or malicious provider adapter
returning bars attributed to the wrong instrument/provider/symbol even
though every other field is well-formed — none of these bars are ever
stored.

## 9. `HistoricalDataService` — the gate pipeline

`HistoricalDataService.fetch_and_store(request)` runs, in this exact
order, stopping at (and returning a structured result for) the first
gate that fails:

1. **Request validation** — `request` must be a real
   `HistoricalBarRequest`; malformed input returns
   `REJECTED_INVALID_REQUEST`.
2. **Olive production tradable-domain gate** — the whole futures
   domain must itself be production-valid, and `request.contract` must
   be one of Olive's required NQ/MNQ instruments (§7); otherwise
   `FAILED` (domain misconfigured) or `REJECTED_NOT_TRADABLE`
   (non-Olive root). **No provider method is ever called before this
   gate passes.**
3. **Provider configuration** — if the configured provider is
   `UnconfiguredHistoricalProvider`, returns `NOT_CONFIGURED` naming
   the exact reason (no provider selected; selected but no API key;
   selected but package not installed).
4. **Network opt-in** — if `OLIVE_HISTORICAL_NETWORK_ENABLED` is not
   `true`, returns `REJECTED_NETWORK_DISABLED` **before calling the
   provider's cost estimator or fetcher at all.**
5. **Cost estimation** — calls `provider.estimate_cost(request)`
   (never the paid fetch itself). A failure here is fail-closed:
   `REJECTED_COST_ESTIMATE_FAILED`, and the paid fetch is never
   attempted.
6. **Cost limit** — the estimate (a `Decimal`, compared via `Decimal`,
   never float) is compared against
   `OLIVE_HISTORICAL_MAX_REQUEST_COST_USD`; exceeding it returns
   `REJECTED_COST_LIMIT` without ever calling the paid fetch. Databento's
   own cost estimate is never called "exact", "guaranteed", or "final"
   anywhere in Olive -- `CostEstimate.estimated_cost_usd` is explicitly
   an approximation to gate against.
7. **Provider fetch** — calls `provider.fetch_bars(request, instrument)`.
   A provider failure returns `FAILED` with the translated error
   message; it is never turned into a fabricated empty success. An
   empty response is legitimate and distinct: `SUCCESS_EMPTY`.
8. **Cross-bar validation** (§8) — a validation failure returns
   `FAILED`.
9. **Storage** — bars are written via `HistoricalBarStore.write_bars`.
   A storage-integrity conflict (already-stored conflicting data)
   returns `FAILED`, naming the conflict; the result is `SUCCESS` only
   once bars are durably written, with `new_records_stored` reporting
   exactly how many records were newly added (an idempotent re-fetch of
   already-stored identical data reports `0`, not an error).

**Phase 3.2 hardening — the store's own output is just as untrusted as
its interface.** `store` is deliberately duck-typed at construction
(§ below), so `write_bars`'s **return value** is checked too: a
malformed return (`None`, a dict, the wrong Olive result type, ...) is
treated as `FAILED` rather than touched as if it were a real
`HistoricalWriteResult` (which would otherwise leak a raw
`AttributeError` the moment `.new_records` is read). An expected
storage I/O failure that escapes as a raw `OSError` (whether from an
injected duck-typed store test double, or any storage path not
otherwise wrapped in its own translation) is also caught here and
turned into `FAILED` — narrowly: any other exception type (a
`TypeError` from calling the store with the wrong signature, say) is a
genuine programming bug and is deliberately left to propagate
unmodified, never silently absorbed into a misleading result.

`HistoricalDataService` never trusts a provider's own reported
identity: if `DatabentoHistoricalProvider.fetch_bars` is ever given an
`instrument` whose `root_symbol` doesn't match the request's contract,
it raises before calling the vendor client at all (defense in depth
alongside the response-level symbol check in §3).

**Phase 3.1 hardening — the provider is untrusted even though it
implements the ABC.** Steps 5 and 7 above no longer assume a
well-behaved provider: after calling `provider.estimate_cost(request)`,
the service checks the return value is actually a `CostEstimate`
instance before using it (a malformed return — `None`, a raw
`Decimal`, a dict, ...) is treated exactly like a provider failure,
`REJECTED_COST_ESTIMATE_FAILED`, never used as if it had the expected
shape. Likewise, after calling `provider.fetch_bars(...)`, the service
checks the return value is actually a `tuple`/`list` **before**
treating a falsy value as the documented "no records" empty-success
case — a broken provider returning `None`/`{}`/`""` is reported as
`FAILED`, never silently mistaken for `SUCCESS_EMPTY`.

**Phase 3.1 hardening — constructor validation.**
`HistoricalDataService.__init__` and `build_historical_provider` now
validate every argument (`provider`, `settings`, and — for the
service — `store`/`registry`/`calendar`) at construction time with
this package's own `InvalidHistoricalServiceConfigurationError`,
never a raw `TypeError` and never deferred until some later call trips
an `AttributeError` on a malformed injected dependency. `store` is
checked by duck-typed interface (a callable `write_bars`), not by
strict `isinstance` against the concrete `HistoricalBarStore` — this
preserves the documented injectability (tests inject a store double
without the real pyarrow-backed implementation) while still rejecting
a blatantly wrong value (`None`, a string, an int, `True`, ...).

## 10. Configuration

| Variable | Default | Meaning |
|---|---|---|
| `OLIVE_HISTORICAL_PROVIDER` | `unconfigured` | `unconfigured` or `databento`. |
| `DATABENTO_API_KEY` | *(empty)* | Never logged, echoed, or included in any error/manifest/health output. |
| `OLIVE_HISTORICAL_DATA_DIR` | `<project root>/data/historical` | Root of the local Parquet store. |
| `OLIVE_HISTORICAL_NETWORK_ENABLED` | `false` | Master kill switch; must be explicitly `true`. |
| `OLIVE_HISTORICAL_MAX_REQUEST_COST_USD` | `0` | Compared via `Decimal` against each request's cost estimate. |

All five defaults are fail-closed: loading settings, importing any
module in this subsystem, running the test suite, running `main.py`,
or constructing a `HistoricalDataService`/provider/store can never, by
itself, cause a network call or a paid request. `Settings.databento_api_key`
uses `field(repr=False)` so it structurally cannot appear in
`repr(settings)`; `Settings.historical_config_summary()` is the only
sanctioned way to describe historical configuration in status output,
and it never includes the key itself.

**Phase 3.1 hardening — `Settings.__post_init__` now self-validates.**
Independent review found that constructing `Settings` **directly**
(bypassing `load_settings()`) with `historical_network_enabled="false"`
(a non-empty, and therefore *truthy*, string) passed straight through
uncaught — `if not settings.historical_network_enabled` evaluated
`False` because the string is truthy, silently defeating the network
kill switch for any caller that built `Settings` by hand (as every
test in this codebase, and any future caller, can do). `Settings` had
no `__post_init__` validation at all before this fix — it was trusted
entirely through its loader. Now every Phase 3 field is validated and
canonicalized unconditionally, regardless of construction path:
`historical_provider` must be a real `HistoricalProviderKind`;
`databento_api_key` must be a `str`; `historical_network_enabled` must
be an actual `bool` (a string, int, or `None` is rejected outright,
never coerced); `historical_max_request_cost_usd` must be a finite,
non-negative `Decimal` (not `bool`/`int`/`float`/`str`). A regression
test (`tests/test_historical_data_config.py`,
`test_string_false_cannot_construct_a_settings_object_at_all`)
reproduces the exact finding and proves it can no longer construct a
usable `Settings` instance at all.

**Phase 3.1 hardening — `historical_data_dir` is cwd-independent.** A
relative `OLIVE_HISTORICAL_DATA_DIR`/`historical_data_dir` previously
resolved against whatever the process's current working directory
happened to be at construction time — meaning the *same* configuration
could silently point at a *different* directory depending on where
`python main.py` was launched from. Olive's chosen policy (the
"preferred" option, rather than requiring an absolute path outright):
`Settings.__post_init__` resolves a relative path against Olive's own
project root (`_PROJECT_ROOT`, derived from this module's own file
location), never the process's cwd, so identical configuration always
resolves to the identical absolute path regardless of invocation
directory (`tests/test_historical_data_config.py`'s
`test_relative_historical_data_dir_identical_from_different_cwds` and
`test_relative_historical_data_dir_resolves_against_project_root_not_cwd`).
An already-absolute path is kept exactly as given.

## 11. Databento adapter

`app/data/providers/databento.py` is the only module in this codebase
that knows Databento's API shape. It is written against the documented
`databento-python` client surface researched for this phase
(`Historical(key=...)`; `metadata.get_cost(dataset=, symbols=, schema=,
start=, end=, stype_in="raw_symbol")`; `timeseries.get_range(...,
stype_in="raw_symbol", stype_out="instrument_id")`; `DBNStore.to_df(
price_type="decimal", tz="UTC")`, which indexes OHLCV rows by
`ts_event`). The `databento` package is imported lazily -- only when
actually constructing a live client, never at module import time or
merely by selecting `OLIVE_HISTORICAL_PROVIDER=databento` -- and a
`client` can be injected directly, which is what every test in
`tests/test_historical_data_databento.py` does, so neither this module
nor its tests require the real package to be installed.

Every expected Databento failure (authentication, permission/
subscription, rate limit, timeout, invalid symbol/schema, data
unavailable, cost-estimation failure) is translated into one of
`HistoricalProviderError`'s subclasses via best-effort status-code and
message classification (`_translate_databento_exception`); only an
exception actually originating from the `databento` package itself
(checked via `type(exc).__module__.startswith("databento")`) is ever
translated this way -- a bug in Olive's own code calling the client
incorrectly (e.g. a `TypeError`) propagates unmodified, never mistaken
for a provider failure. No unbounded retry is implemented anywhere.
`api_key` is never stored as an instance attribute past the
constructor call, so there is nothing on the object for any later
method, repr, or error message to leak.

**Phase 3.1 critical fix — secret-safe vendor error translation.**
Independent review demonstrated that Databento's *own* documented
invalid-Basic-auth error text can itself contain the configured API
key, in the literal shape `"Invalid username in Basic auth ('<key>')"`
— and the pre-3.1 `_translate_databento_exception` interpolated
`str(exc)` directly into the message it raised, which could then
surface the key through a `CostEstimationFailedError`/`HistoricalFetchResult.message`/
health-check detail/log line/traceback. `_translate_databento_exception`
now **only** returns one of a small, fixed set of pre-written generic
category messages (e.g. `"Databento authentication failed (invalid or
rejected API key)."`) — the vendor exception's status code and message
text are inspected internally, purely to pick a category, and are
**never** echoed into anything this module raises. Every translated
error is additionally raised with `from None`, deliberately severing
the exception chain, so that even printing a full traceback can never
surface the original vendor exception object or its text. A `400`
status carrying Databento's documented invalid-key message is
classified as `ProviderAuthenticationError` (not a generic
`ProviderUnavailableError`) by matching on the message's own documented
keywords (`"invalid username"`, `"basic auth"`, `"invalid api key"`,
`"bad api key"`), independent of status code. Regression tests in
`tests/test_historical_data_databento.py`
(`test_fetch_bars_400_status_invalid_key_message_classified_as_auth_and_key_never_leaks`,
`test_estimate_cost_400_status_invalid_key_message_never_leaks`)
embed the real documented vendor-message shape with a fake API key and
assert the key is absent from every rendering of the raised exception.

**Phase 3.1 fix — safe volume conversion.** The adapter previously
used `int(row.volume)`, which silently truncates a fractional provider
volume (`1.5` → `1`) — undetectable data corruption. `_safe_volume`
now uses `operator.index()` (after explicitly rejecting `bool` first,
since `bool.__index__` exists and would otherwise accept `True`/`False`
as `1`/`0`), which accepts a true integer-like value (including a
numpy integer scalar, via duck typing) and rejects every float
(including a whole-number float like `1.0`), `NaN`/`Infinity`, and a
numeric string, raising `ProviderDataError` instead of silently
truncating.

**Phase 3.1 fix — provider row-data-quality failures are structured,
not leaked.** A tick-misaligned price, bad volume, or other
`HistoricalDataError` raised while building a `HistoricalBar` from a
provider row (via `HistoricalBar.from_decimal_prices`) is now caught
and re-raised as `ProviderDataError` — a `HistoricalProviderError`
subclass `HistoricalDataService`'s `except HistoricalProviderError`
around `fetch_bars` actually catches — rather than escaping as a raw
`HistoricalDataError` the service does not expect from this call.

**Phase 3.1 fix — narrower `to_df()` exception handling, `ts_event`-
exact, `map_symbols=True` explicit.** `to_df()` is now called with
`map_symbols=True` explicitly (never left to the library default), and
a missing/`None` resolved `symbol` on any row fails closed
(`ProviderResponseIdentityError`) rather than silently skipping the
identity check. The response `DataFrame`'s index name must be exactly
`"ts_event"` — accepting `"ts_recv"` (receive time) would silently
relabel receive time as event time. Only a genuine Databento-originated
decode failure from `to_df()` is translated into
`ProviderUnavailableError`; an unrelated programming error (Olive
calling `to_df()` with a signature it doesn't support) propagates
unmodified, never disguised as a provider outage.

**Phase 3.2 critical fix — a response row missing `instrument_id` must
fail closed, never fabricate provenance.** Independent review found
that a response row with **no** `instrument_id` at all (a dropped
column, or a `None`/`NaN`/empty value) was previously accepted, with
`HistoricalBar.provider_instrument_id` silently set to the
symbology-resolved **expected** instrument ID instead — defeating the
entire point of the defense-in-depth per-row check by substituting
Olive's own expectation for data Databento never actually reported.
Every response row must now include a present, non-blank
`instrument_id` (checked for `None` and for `NaN`, which is how a
missing value actually reads back from a pandas column) that matches
the resolved expected ID exactly, or `ProviderResponseIdentityError`/
`ProviderSymbologyError` is raised before any bar is constructed — the
stored `provider_instrument_id` is always the row's own reported
value, never a fallback.

**Phase 3.2 critical fix — symbology end-date coverage for an
intraday request end-time.** Databento's `symbology.resolve` dates are
a half-open `[start_date, end_date)` **calendar-date** interval, but
`request.end` is a half-open **datetime** that can fall at any
time-of-day. The previous date projection only extended
`request_end_date` by one day when it was `<=` the start date (the
same-calendar-day case); a genuinely cross-day request with an
intraday end time (e.g. `start=2026-09-30T23:00Z`,
`end=2026-10-01T00:30Z`) left `request_end_date=2026-10-01`
**unchanged** — which, being exclusive, covers **none** of October
1st, even though 00:00–00:30 on October 1st is squarely inside the
requested half-open datetime interval. The fix mirrors
`app.data.storage._months_between`'s own half-open-boundary reasoning:
`request.end`'s own calendar date still contains requested instants
unless `request.end` is **exactly** that date's first instant
(`00:00:00.000000`); only then does its own date contribute zero
requested instants. Every other case extends the date interval one day
past `request.end`'s own date. Regression tests in
`tests/test_historical_data_databento.py` spy on the exact
`symbology.resolve` arguments for: same-day intraday, cross-midnight
intraday-end, end-exactly-midnight (no over-extension), end one
microsecond past midnight, cross-month, and cross-year cases.

**Phase 3.2 fix — the injected client is itself a public boundary.**
`DatabentoHistoricalProvider.__init__`'s `client=` dependency-injection
seam previously accepted anything (`"bad"`, an object missing expected
attributes, ...) and only failed later with a raw `AttributeError` the
first time `estimate_cost()`/`fetch_bars()` touched a missing
attribute. The constructor now validates the **callable interface**
this adapter actually needs (`metadata.get_cost`,
`timeseries.get_range`, `symbology.resolve`) and raises
`ProviderNotConfiguredError` for a client missing any of them — a
strict `isinstance` against Databento's concrete client class is
deliberately NOT used, since test doubles implementing only this
interface are an intentional, valuable part of this adapter's design.

**Databento version/API drift note:** the real `databento` package
could not be installed in this build sandbox (see §14) and so this
adapter's behavior against the actual vendor service has not been,
and could not be, exercised here. It was verified against an injected
fake client that mimics the documented interface exactly (constructor
signature, method names, keyword arguments, DataFrame shape). If the
real client's signature drifts in a future package version, only this
file should need to change.

## 12. Local Parquet storage

`app/data/storage.py` / `HistoricalBarStore`. Deterministic partition
layout, never dependent on the current working directory, a random
UUID, or a secret value:

```
<root_dir>/<provider>/<dataset>/<root_symbol>/<contract_identity>/<timeframe_schema>/year=YYYY/month=MM/
    bars.parquet
    manifest.json
```

e.g. `data/historical/databento/GLBX.MDP3/NQ/NQ-2026-12/ohlcv-1m/year=2026/month=09/`.
**The year/month is the bar data's own calendar month (`ts_event`), not
the contract's expiration month** — a December-2026 contract's
September-2026 trading activity is stored under `year=2026/month=09`.

- **Idempotency / conflicts:** writing the same or an overlapping
  request twice never duplicates records (bars sharing a canonical key
  with identical content merge to one); bars sharing a key with
  conflicting content raise `HistoricalStorageIntegrityError` for
  **every** affected partition before any partition in that call is
  written — a conflict discovered while planning partition 2 of 3
  leaves partition 1 untouched even though it would otherwise have
  been fine to write (two-phase plan-then-commit).
- **Atomicity:** each partition's Parquet file and manifest are written
  to a temp file in the same directory and atomically renamed into
  place (`os.replace`, with `fsync`); a failure partway through cleans
  up its temp file and never leaves a corrupted or half-written file
  where a good one used to be.
- **Manifest** (`manifest.json`, one per partition): `schema_version`,
  `olive_contract_identity`, `provider_raw_symbol`, `root_symbol`,
  `contract_year`, `contract_month`, `provider`, `dataset`, `timeframe`,
  `partition_year`, `partition_month` (Phase 3.3 — the partition
  directory's own calendar identity, verified against both the
  directory being accessed and, independently, the bars actually
  inside it; see below), `requested_start_utc`, `requested_end_utc`,
  `actual_first_timestamp_utc`, `actual_last_timestamp_utc`,
  `record_count`, `data_label`, `last_written_at_utc`, `tick_size`,
  `contract_multiplier`, `estimated_cost_usd`, `storage_format`,
  `checksum_sha256` (SHA-256 of the Parquet file's bytes). **Never**
  includes an API key, credential, or any other secret.
- **Reads** (`read_bars`) are a purely local filesystem operation
  requiring no provider, API key, or network access; a half-open
  `[start, end)` range is applied after loading each overlapping
  monthly partition. `HistoricalReadResult.partitions_found == 0`
  (nothing was ever stored) is distinguished from `partitions_found > 0`
  with an empty `bars` tuple (partitions existed, but nothing in them
  fell inside the requested sub-range) -- both are explicit, legitimate
  "no data" outcomes, never ambiguous with a storage failure (which
  raises instead).
- **Path safety:** every path segment (`provider`/`dataset`/
  `root_symbol`/`contract_identity`/timeframe schema) is checked against
  path-traversal characters and `.`/`..` before being joined, and the
  final resolved partition path is confirmed to still be inside the
  storage root -- defense in depth even though every current caller
  already supplies pre-validated canonical values.
- **Third-party import safety:** `pyarrow` is imported lazily inside
  the methods that need it; constructing a `HistoricalBarStore`, or
  importing this module, never requires `pyarrow` to be installed.
- **Phase 3.1 fix — transactional single-partition write (§30).** The
  `bars.parquet` + `manifest.json` pair was previously written as two
  *separate* atomic operations: if the manifest write failed (or never
  even started) after the parquet write had already succeeded, the
  partition was left with a NEW parquet file paired with a STALE or
  missing manifest — a mismatched pair. `_write_partition_file` now
  captures the previous valid pair's bytes before writing anything,
  and if the manifest write fails for any reason, rolls `bars.parquet`
  back to that previous state (or removes it, if there was no previous
  pair at all) before re-raising — a partition is now always left with
  either the complete previous valid pair or the complete new valid
  pair, never a mismatched mix.
- **Phase 3.1 fix — cross-partition rollback (§31).** The existing
  two-phase plan-then-commit design already prevented a data
  *conflict* discovered while planning partition 2 from touching
  partition 1 at all — but it said nothing about an I/O error raised
  while *committing* a later partition after an earlier one had
  already been written successfully in the same `write_bars` call.
  `write_bars` now records each partition's pre-call state as it
  commits, and if any partition's write raises, rolls back **every**
  partition already written during that same call to its pre-call
  state before re-raising — one `write_bars` call is never left
  half-committed across partitions.
- **Phase 3.1 fix — schema-version enforcement, both directions
  (§24).** `write_bars` now refuses to persist a bar whose
  `schema_version` is not one this build recognizes
  (`_SUPPORTED_BAR_SCHEMA_VERSIONS`, currently just the current
  `OLIVE_HISTORICAL_BAR_SCHEMA_VERSION`); `read_bars` now refuses to
  interpret a stored row, or trust a manifest, declaring an
  unsupported `schema_version` — previously any positive int was
  silently accepted on read.
- **Phase 3.1 fix — manifest is actually verified on read, not just
  written (§28).** `read_bars` now calls `_verify_partition_manifest`
  for every partition before trusting it: the manifest must exist, its
  `schema_version` must be supported, its recorded identity
  (`provider`/`dataset`/`root_symbol`/`olive_contract_identity`/
  `contract_year`/`contract_month`/`timeframe`) must match the
  partition being read, and `checksum_sha256` must match
  `bars.parquet`'s **actual current bytes on disk** (recomputed at
  read time, not merely trusted from write time) — any discrepancy
  raises `HistoricalStorageIntegrityError` rather than returning data
  that may be corrupted, tampered with, or belong to a misplaced file.
- **Phase 3.1 fix — per-bar identity cross-check on read (§27).** Even
  with a verified manifest, every individual bar loaded from a
  partition has its own `root_symbol`/`contract_year`/`contract_month`/
  `timeframe`/`provider`/`dataset` checked against the requested
  partition identity, as defense in depth against a misplaced or
  corrupted file whose manifest happens to pass.
- **Phase 3.1 fix — naive stored timestamps fail closed (§26).** A
  `ts_event` read back from Parquet without timezone info previously
  was silently assumed to be UTC. `_normalize_stored_timestamp` now
  raises `HistoricalStorageIntegrityError` for a naive (or non-`datetime`)
  stored timestamp instead — Olive's schema always writes UTC-aware
  timestamps, so a naive one read back means the data is corrupt or
  was written by an incompatible schema, never a value to guess at.
- **Phase 3.1 fix — half-open month-boundary correctness (§29).**
  `_months_between(start, end)` previously always included `end`'s own
  calendar month, even when `end` landed exactly on that month's first
  instant (e.g. `2026-10-01T00:00:00Z`) — a case where, because the
  requested range is half-open (`[start, end)`), nothing in that month
  could ever actually match. The month is now excluded in that exact
  case (and the exclusion correctly carries across a year boundary).
  Covered by real, unconditionally-running tests in
  `tests/test_historical_data_storage_pure.py` (see §14 — this file
  needs no `pyarrow` at all, since the function is pure datetime
  logic, so these tests are NOT part of the pyarrow-gated skip below).
- **Phase 3.1 fix — public-API boundary hardening (§19-21).**
  `write_bars(None)`/`write_bars(123)` no longer leak a raw `TypeError`
  from `tuple(bars)`; `write_bars`'s `requested_start`/`requested_end`/
  `estimated_cost_usd`/`contract_multiplier` are now validated (aware
  UTC datetimes with `start < end`; finite non-negative/strictly
  positive `Decimal`s) before any manifest metadata is built from them.
- **Phase 3.1 fix — `read_bars` no longer silently coerces malformed
  identity fields (§20).** `contract_year=True`/`contract_month=3.5`/
  a numeric string/`contract_month=True` are all now rejected outright
  (`HistoricalStorageError`) rather than being silently coerced by
  `int(...)` (`int(True) == 1`, `int(3.5) == 3`, `int("12") == 12`);
  `contract_month` must be exactly one of Olive's quarterly months
  (`3`, `6`, `9`, `12`).
- **Phase 3.2 critical fix — `write_bars` now verifies an existing
  partition's manifest/checksum before merging onto it, exactly like
  `read_bars` already did.** Previously, `write_bars`'s own internal
  read of an existing `bars.parquet` for merge purposes bypassed
  `_verify_partition_manifest` entirely — a `bars.parquet` dropped into
  a partition directory with **no** `manifest.json` (or a tampered
  one) was read, merged, and then given a **brand-new trusted
  manifest**, silently legitimizing previously unverified or corrupt
  data. `write_bars` now requires the same manifest existence, schema
  version, identity, and checksum verification as `read_bars` before
  trusting any existing partition as merge input, and rejects (never
  silently repairs) an orphaned partition (`bars.parquet` with no
  manifest, or a manifest with no `bars.parquet`) with
  `HistoricalStorageIntegrityError`.
- **Phase 3.2 fix — existing stored CONFLICTING duplicates are
  rejected before merge, never silently resolved.** `_merge_bars`'s
  existing-bars loading never checked whether the EXISTING stored data
  itself already contained two different bars for the same canonical
  key (only possible via corruption or an out-of-band write) — a
  corrupt file like that would have one bar silently overwrite the
  other on the next write. `write_bars` now runs the same
  conflict-detection logic against freshly-read existing bars before
  merging, raising `HistoricalStorageIntegrityError` rather than
  picking a winner.
- **Phase 3.2 fix — a rollback failure is never silently swallowed.**
  Both the single-partition (manifest-write-failure) and
  cross-partition commit-failure rollback paths previously caught a
  rollback failure with a bare `except Exception: pass` — meaning
  Olive could report a clean `FAILED`/exception while the rollback
  itself had ALSO failed, leaving storage inconsistent with no signal
  that operator intervention is needed. Both paths now collect any
  rollback failure and, when one occurs, raise
  `HistoricalStorageIntegrityError` chaining **both** the original
  write failure and the rollback failure, explicitly naming the
  affected partition(s) and stating that operator intervention may be
  required — the original failure is never masked, and a rollback
  failure is never hidden either.
- **Phase 3.2 fix — `HistoricalWriteResult`/`HistoricalReadResult` are
  now self-validating.** Independent review constructed
  `HistoricalWriteResult(partitions_written=-1, new_records=-2,
  total_records=-3)` and `HistoricalReadResult(bars=(),
  partitions_found=-1)` successfully — both now validate every field
  (true non-negative ints; `bars` normalized to a tuple of real
  `HistoricalBar` instances) in `__post_init__`, including the
  cross-field invariant `new_records <= total_records`, raising
  `HistoricalStorageError` rather than permitting an impossible result
  object to exist at all.
- **Phase 3.2 fix — the storage root is CWD-independent even when
  `HistoricalBarStore` is constructed directly with a relative path.**
  A relative `root_dir` was previously stored verbatim and re-resolved
  against the process's CURRENT working directory on every later
  `_partition_dir` call — so the SAME store instance could silently
  resolve to two different physical locations before and after an
  unrelated `os.chdir()` elsewhere in the process, contradicting this
  module's own documented guarantee. `__init__` now resolves to an
  absolute path exactly once, at construction time.
- **Phase 3.2 fix — storage I/O/serialization failures become
  structured errors, narrowly.** `_atomic_write_bytes`'s own `OSError`
  (disk full, permission denied, a removed parent directory, ...) and
  the Parquet serialization call (`pq.write_table`) are now translated
  into `HistoricalStorageError` at their own narrow boundaries — never
  a raw third-party/filesystem exception escaping this module's public
  methods, and never a broad `except Exception` that would also catch
  a genuine Olive programming bug.
- **Phase 3.3 critical fix — pre-existing IDENTICAL duplicates no
  longer risk a negative `new_records` count / a post-commit result
  construction failure.** A partition that already (legitimately, per
  the tolerated-duplicate policy below) holds two identical copies of
  one bar previously contributed its RAW row count (2) as
  `existing_count` — so re-submitting that same bar again computed
  `len(merged) - existing_count == 1 - 2 == -1`, which
  `HistoricalWriteResult.__post_init__` then rejected **after** the
  partition had already been durably committed to disk, leaving a
  successful write masquerading as a crashed call. `write_bars` now
  canonicalizes (deduplicates) an existing partition's bars **before**
  computing `existing_count`/`new_records`/`total_records` — every
  quantity `HistoricalWriteResult` could possibly reject is now fully
  determined by a read-only planning pass, before any partition in the
  call is written, making a post-commit construction failure
  structurally impossible, not merely less likely.
- **Phase 3.3 fix — one explicit, shared duplicate-canonical-key
  policy, enforced identically by both `write_bars` and `read_bars`.**
  `_canonicalize_bars` is the single place "what happens when the same
  canonical key appears more than once" is decided: identical content
  de-duplicates to one record; conflicting content raises
  `HistoricalStorageIntegrityError`, never silently picking a winner.
  Applied to an existing partition's bars before they are trusted as
  write-side merge input, to a partition's own decoded bars before
  `read_bars` returns them, and — as defense in depth — to the full
  cross-partition result `read_bars` is about to return, so a
  `HistoricalReadResult` can never itself contain two bars for the
  same canonical key even if they somehow came from two different
  partition files.
- **Phase 3.3 fix — `read_bars` now rejects an orphan manifest
  (manifest present, parquet missing) instead of treating it as "this
  partition was never written."** Previously only the reverse orphan
  direction (parquet with no manifest) was rejected on read; a
  manifest with no accompanying `bars.parquet` silently fell through
  to `continue` and was indistinguishable from a genuinely
  never-written partition.
- **Phase 3.3 critical fix — a partition's `year=YYYY/month=MM`
  directory identity is now verified against the bars actually inside
  it, independent of what the manifest claims about itself.**
  Independent reproduction copied a whole valid, checksum-matching
  parquet+manifest pair from one month's partition directory into
  another month's directory: every existing identity/checksum check
  passed (the pair really was internally self-consistent), and the
  misplaced bar was merely filtered out by the half-open-range filter
  at the end of `read_bars` — silently turning corruption into an
  innocent-looking empty result. Two independent layers now guard this:
  (1) the manifest's own self-declared `partition_year`/
  `partition_month` fields (new manifest keys, §below) are checked
  against the directory being accessed, and (2) every individual
  loaded bar's **own** `ts_event.year`/`.month` is checked against that
  same directory, regardless of what the manifest claims — a forged
  manifest that correctly self-reports the target directory's
  year/month while the actual bar content is still misplaced is still
  caught by layer (2) alone. Enforced on both `write_bars` (before
  trusting an existing partition as merge input) and `read_bars`
  (before trusting or returning any bar).
- **Phase 3.3 fix — the manifest now stores `partition_year`/
  `partition_month` explicitly,** verified on both read and
  merge-write (layer (1) above). No migration compatibility is needed
  for this new field — there is no committed/released production
  dataset predating it yet.
- **Phase 3.3 fix — manifest content is now cross-checked against
  what was actually decoded from the partition, not only the raw-bytes
  checksum.** `_verify_manifest_content_matches_bars` compares
  `record_count`, `actual_first_timestamp_utc`/
  `actual_last_timestamp_utc`, `provider_raw_symbol`, `tick_size`,
  `data_label`, `schema_version`, and `storage_format` against the
  partition's actually-decoded bars, raising
  `HistoricalStorageIntegrityError` on any disagreement — a manifest
  that was written with rich descriptive fields but never subsequently
  cross-checked against its own partition's content was not a real
  integrity guarantee for those fields. This check (like the
  partition-period and coherence checks above) always runs against the
  **raw, pre-canonicalization** decoded rows, so a tolerated
  pre-existing identical duplicate (2 raw rows, 1 unique bar) is never
  mistaken for manifest/content disagreement — `record_count` means
  "rows actually in this file," not "unique canonical keys."
- **Phase 3.3 fix — one partition must represent one coherent market
  series, enforced as an explicit invariant distinct from the write-side
  grouping key.** The key `write_bars` already groups new bars by
  (`provider`, `dataset`, `root_symbol`, `contract_identity`,
  `timeframe`, year, month) only guarantees agreement on *those*
  fields — `tick_size`, `provider_raw_symbol`, `provider_instrument_id`,
  `data_label`, and `schema_version` are not part of it. Independent
  reproduction successfully wrote (and later read back) two
  individually well-formed bars into the same partition with different
  `tick_size`/`provider_raw_symbol`/`provider_instrument_id` — each
  bar was fine on its own, but the partition was internally
  incoherent. `_check_partition_coherence` now enforces agreement on
  all five fields across every bar in a partition, on every **write**
  (an existing partition's raw bars before merge, and the final merged
  result before any commit) and every **read** (before any bar from an
  existing partition is trusted or returned) — storage is a trust
  boundary in both directions.
- **Phase 3.3 fix — Databento symbology instrument-ID resolution is
  now strictly validated before any paid fetch.** Every resolved
  instrument ID must be an actual `str` (never `int`/`float`/`bool`/
  any other type, even though the underlying identifier is documented
  as an unsigned integer — Databento's own symbology response
  represents it as a string), non-empty after stripping, composed only
  of ASCII decimal digits (explicitly guarding against `str.isdigit()`
  accepting non-ASCII digit characters), and strictly positive — `None`,
  `""`, `"   "`, `0`, `True`, `False`, any float, and non-digit text
  are all rejected with `ProviderSymbologyError` before
  `timeseries.get_range()` is ever called. Each symbology mapping
  entry's own shape is validated first, separately: `None`/`{}`/`[]`/a
  bare string/number/bool is rejected as a malformed entry with the
  same Olive-owned error, never a raw `AttributeError`/`TypeError`.
- **Phase 3.3 fix — `HistoricalBar`'s tick-count/volume fields now
  enforce an explicit storage-representability upper bound,
  `ARROW_INT64_MAX = 2**63 - 1`.** This is the exact signed-64-bit
  integer range Olive's own Parquet schema declares for these columns
  — a storage-representation bound, not an arbitrary economic cap —
  so a value `HistoricalBar` would accept but could never actually be
  written to Parquet now fails at construction instead of at
  `_bars_to_table` time.
- **Phase 3.3 fix — `_bars_to_table`'s actual Arrow conversion calls
  (`pa.array`/`pa.Table.from_arrays`) now translate an expected Arrow
  representation failure into `HistoricalStorageError`,** scoped
  **narrowly** to just those two calls — the per-bar Olive-attribute-
  gathering loop immediately above them is deliberately left outside
  the `try`/`except`, so a genuine Olive bug there (e.g. a typo'd
  attribute access) still propagates unmodified, exactly mirroring the
  existing `pq.write_table` translation pattern.
- **Phase 3.3 fix — `HistoricalWriteResult` gained one more
  self-validation invariant:** `partitions_written` can never exceed
  `total_records` — every partition reported as written must contain
  at least one record. `HistoricalFetchResult` with status `SUCCESS`
  now also requires a real non-negative `new_records_stored` int
  (never `None`) — a successful fetch always durably stores and
  reports a count.

## 13. Health reporting

`Historical market data` in `python main.py`'s status report (and
`app.health.get_system_health`) is a real check, not a hard-coded
string:

- `NOT_CONFIGURED` — no provider selected (the default), or a selected
  provider is missing required configuration (no API key, or the
  `databento` package is not installed) — the exact reason is named.
- `CONFIGURED` — a real provider was actually constructed (provider
  selected, API key present, vendor package importable); the detail
  notes whether network fetches are enabled or disabled and the
  configured cost limit, but never the key itself.
- `ERROR` — configuration is present but broken (e.g. an API key that
  is only whitespace, which `DatabentoHistoricalProvider` itself
  rejects as empty even though `Settings.has_databento_api_key`'s
  truthiness check alone would have missed it).

This check **never** calls any Databento network method
(`metadata.get_cost`/`timeseries.get_range`) — verified in
`tests/test_historical_data_health.py` by injecting a client whose
methods raise `AssertionError` if ever touched, including in the
`CONFIGURED` case.

## 14. Known limitations of this build

This cloud workspace has no PyPI/package-registry network access at
all (confirmed via `pip`, `uv pip`, `apt-get`, and a direct `curl` to
`pypi.org`, all refused). **Neither `databento` nor `pyarrow` could be
installed in this sandbox.** Concretely, this means:

- `tests/test_historical_data_storage.py` is written to run for real,
  unmodified, against real `pyarrow` on any machine where it installs
  (the user's own machine, CI) — via `pytest.importorskip("pyarrow")`
  at module scope, the **entire file reports as one `skipped` entry**
  (not failed) in this sandbox; none of its individual test functions
  actually execute here. **This status is reported honestly in every
  completion report — it is never described as "passed."**
  - Two of this module's pure-logic helpers, `_months_between` (the
    Phase 3.1 §29 half-open-boundary fix) and `_normalize_stored_timestamp`
    (the Phase 3.1 §26 naive-timestamp fix), need no `pyarrow` at all
    (importing `app.data.storage` itself never requires it — only
    `_import_pyarrow()`, called lazily inside the methods that need
    it, does). These are covered by a **separate, un-gated test file**,
    `tests/test_historical_data_storage_pure.py`, which **does
    genuinely execute and pass** in this sandbox (13 tests) — this is
    real, not supplemental, coverage, and is called out separately in
    every completion report from the pyarrow-gated skip above so the
    two are never conflated.
  - The rest of `HistoricalBarStore`'s orchestration logic (two-phase
    partitioning and conflict handling, idempotent merge, single- and
    cross-partition atomic-write rollback — including a rollback
    *failure* itself — manifest writing AND on-read AND on-write-merge
    verification, checksum computation and verification, schema-version
    enforcement, path-traversal defense, read-time identity
    cross-checks, result-object self-validation, and CWD-independent
    root resolution) is exercised by a **shipped, permanent** regression
    file, `tests/test_historical_data_storage_transaction.py` (53
    tests, all passing as of this correction, up from 26 as of Phase
    3.2 — the Phase 3.3 additions formalize every scenario this
    correction's own manual adversarial verification scripts
    reproduced, as permanent pytest tests: pre-existing identical
    duplicates never going negative, orphan manifests on read,
    misplaced-month partitions with a forged manifest, mixed
    tick_size/provider_raw_symbol/provider_instrument_id coherence
    violations on both write and read, and `_bars_to_table`'s own
    Arrow-conversion exception translation) — this closes the
    Phase 3.1 gap where equivalent coverage existed only in an
    unshipped scratch-space harness. It monkeypatches
    `app.data.storage._import_pyarrow` to return a minimal,
    self-contained fake of the handful of `pyarrow`/`pyarrow.parquet`
    calls `storage.py` actually makes
    (`tests/_fake_pyarrow.py`, pure Python + `pickle`, no real
    `pyarrow` involved), so the REAL `HistoricalBarStore` production
    code runs end-to-end, in every environment, unconditionally. This
    is still **not**, and must never be reported as, a substitute for
    running the real `tests/test_historical_data_storage.py` against
    the real `pyarrow` package (binary Parquet format compatibility,
    real `pyarrow` exception shapes, real performance characteristics
    are all still unverified here) — that file remains separately
    authoritative and unmodified, and a real run of it with **zero**
    skips, on the user's own machine, is what finally closes this out.
- `tests/test_historical_data_databento.py` injects a fake client via
  the adapter's `client=` constructor parameter in every test, so it
  runs and passes fully in this sandbox — but the real `databento`
  package's actual method signatures/behavior have not been, and could
  not be, exercised directly here. `pandas` happens to already be
  present in this sandbox's base image (used for the fake-client
  DataFrame fixtures), so the adapter's DataFrame-handling logic itself
  was exercised; only the real vendor HTTP client was not.
- `requirements.txt` declares `databento`/`pyarrow`/`pandas` version
  ranges based on research (WebSearch/WebFetch against Databento's
  documentation and GitHub source, and — as of Phase 3.2 — each
  package's own PyPI project page, confirming the current stable
  releases as of this correction are `databento` 0.87.0 and `pyarrow`
  25.0.1) rather than a pin verified by actually running
  `pip install` successfully in this environment (re-confirmed during
  Phase 3.2: `pip install pyarrow==25.0.1`/`databento==0.87.0` both
  still fail with "Could not find a version that satisfies the
  requirement" — no PyPI network access exists in this sandbox, only
  the narrower allowlisted proxy WebSearch/WebFetch use). Phase 3.2
  narrowed both ranges from the deliberately wide Phase 3.1 estimates
  (`databento>=0.40,<1`; `pyarrow>=14,<19`, which independent review
  correctly pointed out would needlessly block installing either
  package's own current release) to target that confirmed current API
  line specifically — still not a tested guarantee, only a narrower,
  more honest starting point.

None of this affects the storage/validation/service/health logic that
*could* be verified without those packages (the large majority of this
subsystem), and none of it changes Phase 3's safety posture — every
gate that matters (tradable-domain check, network opt-in, cost limit)
lives in code that was fully exercised. It does mean the two
third-party integration points (real Parquet I/O, real Databento HTTP
calls) carry a residual "verified against a faithful fake, not the
real package" caveat that should be closed out by running this
project's test suite with `pip install -r requirements.txt` fully
succeeded, on a machine with normal package-registry access, before
relying on this build for a real paid fetch.

## 15. What a real fetch actually requires

For `HistoricalDataService.fetch_and_store` to ever reach a real
Databento network call, **all** of the following must be true
simultaneously: `OLIVE_HISTORICAL_PROVIDER=databento`;
`DATABENTO_API_KEY` set to a real key; `OLIVE_HISTORICAL_NETWORK_ENABLED=true`;
a structurally valid `HistoricalBarRequest` for `NQ` or `MNQ`; Olive's
futures domain itself production-valid; and the provider's own cost
estimate no greater than `OLIVE_HISTORICAL_MAX_REQUEST_COST_USD`. Every
one of these defaults closed, and this document's §9 pipeline is the
only code path that can reach a paid call. As of Phase 3.1, a sixth
condition is implicit in "a structurally valid request": the raw
symbol's Databento symbology resolution must agree between the
contract's own month and the request's actual date range (§3) — a
request that would otherwise fetch a different contract-year's data
under Olive's requested identity is rejected before this point.

## 16. Phase 3.1 correction summary

An independent review of the delivered Phase 3 build (778 tests
passing, 1 skipped, `python main.py` successful, no Phase 4 scope)
found several substantive safety/data-integrity issues, all fixed in
this corrective pass with regression tests. In order of severity:

1. **Settings network-kill-switch bypass (critical).** Direct
   `Settings` construction with `historical_network_enabled="false"`
   (a truthy string) could silently defeat the network kill switch —
   fixed with `Settings.__post_init__` validation (§10).
2. **API-key leak via vendor exception text (critical).**
   Databento's documented invalid-auth error message can itself
   contain the API key — fixed with secret-safe, category-only error
   translation and severed exception chains (§11).
3. **Raw-symbol decade-reuse / contract-identity corruption
   (critical).** `NQZ6` means a different contract in 2016/2026/2036;
   the prior response-level symbol check alone could not catch a
   stale/misdirected request receiving an honestly-self-reporting but
   wrong-decade response — fixed with a `symbology.resolve`-based
   identity-safety gate before any paid fetch (§3, §11).
4. **Fractional provider volume silently truncated.** `int(1.5) == 1`
   — fixed with `operator.index()`-based safe conversion (§11).
5. **Transactional storage gap.** The `bars.parquet` + `manifest.json`
   pair was not atomic as two separate writes, and a cross-partition
   commit failure could leave an earlier partition already updated —
   fixed with single-partition and cross-partition rollback (§12).
6. **Manifest/checksum written but never verified on read.** Fixed —
   `read_bars` now verifies manifest existence, schema version,
   identity, and a freshly-recomputed checksum before trusting any
   partition (§12).
7. Cost-estimate domain-contract gap, provider-return-type validation
   (`estimate_cost`/`fetch_bars`), provider row-data-quality failures
   leaking past the service's `HistoricalProviderError` catch,
   `to_df()` exception-handling breadth, `ts_recv`-vs-`ts_event`
   strictness, `map_symbols=True` explicitness — all fixed in the
   service (§9) and adapter (§11).
8. A dozen public-boundary hardening fixes across `HistoricalBar`/
   `HistoricalFetchResult` (impossible-state construction),
   `HistoricalDataService`/`build_historical_provider` constructors,
   `normalize_and_validate_bars`, and `HistoricalBarStore.write_bars`/
   `read_bars` (raw `TypeError` leaks, silent bool/float/string
   coercion of identity fields, schema-version enforcement, naive-
   timestamp handling, half-open month-boundary correctness) — see
   §6, §9, §12, §14.

ZIP packaging was also corrected: `olive-phase3.zip` had nested every
file under an erroneous `olive-ai/` directory prefix instead of
placing them at the archive root (the Phase 2.5 convention); this
build's `olive-phase3.1.zip` places `main.py`/`app/`/`config/`/
`tests/`/`docs/`/etc. directly at the archive root, verified by
extracting it to a clean directory and inspecting the result.

No Phase 4 scope was introduced in this corrective pass, and no commit
or push was made from this workspace — see the completion report's
"Git Status" section.

## 17. Phase 3.2 correction summary

A second independent review, of the actual delivered `olive-phase3.1.zip`
(925 tests passing, 1 skipped, `python main.py` successful, correct ZIP
root layout, every headline Phase 3.1 fix confirmed present), found
further adversarially-discovered public-boundary and storage-integrity
gaps, all fixed in this corrective pass with regression tests. In
order of severity:

1. **Databento response-identity fabrication (critical).** A response
   row missing `instrument_id` entirely was silently given the
   symbology-resolved *expected* value instead of failing closed,
   defeating the whole point of the defense-in-depth check — fixed;
   `provider_instrument_id` is now always the row's own reported
   value, never a fallback (§11).
2. **Symbology end-date off-by-one for an intraday request end-time
   (critical).** A cross-day request ending partway through a day
   (e.g. `...T00:30Z`) resolved symbology over a date range that did
   not cover that day at all, defeating the raw-symbol decade-reuse
   protection for exactly the requests it is supposed to protect —
   fixed with correct half-open date-interval projection (§11).
3. **Production economics/provenance never cross-checked.** A bar
   with the wrong `tick_size`, the wrong `provider`, or the wrong
   `provider_raw_symbol` — individually self-consistent, but
   production-wrong — was accepted and stored. Fixed:
   `normalize_and_validate_bars` now cross-checks every bar against
   Olive's already production-validated instrument and the provider
   actually servicing the request (§8).
4. **`write_bars` could legitimize unverified/corrupt existing data.**
   Its internal read of an existing partition for merge purposes
   bypassed the same manifest/checksum verification `read_bars`
   already enforced — fixed (§12).
5. **Result-object impossible states.** `HistoricalWriteResult`/
   `HistoricalReadResult` could be constructed with negative counts or
   an internally inconsistent `new_records > total_records` — fixed
   with `__post_init__` validation on both (§12).
6. **Rollback-failure masking.** A rollback failure during either
   single- or cross-partition recovery was silently swallowed — fixed;
   both paths now raise a dedicated integrity error naming both the
   original and rollback failures (§12).
7. A dozen further public-boundary/storage-exception-hygiene fixes:
   `HistoricalBar.conflicts_with` validating its argument,
   `provider_instrument_id` rejecting empty/whitespace strings, an
   extreme-numeric Decimal-overflow translation, the injected
   Databento client's interface validated at construction, the
   storage root resolved CWD-independently even on direct
   construction, `HistoricalDataService` validating `write_bars`'s
   own return type and translating an expected `OSError` from
   storage into a structured `FAILED` result, and narrow
   `HistoricalStorageError` translation at the filesystem/Parquet-
   serialization boundary — see §6-§9, §11, §12.
8. Dependency-range honesty: Phase 3.1's `databento`/`pyarrow` ranges
   were unnecessarily wide (would have blocked installing either
   package's own current release); narrowed to target the current
   confirmed stable release line (§14).
9. Permanent shipped regression coverage for every Phase 3.1/3.2
   storage-transaction behavior that previously existed only in an
   unshipped scratch-space harness (`tests/test_historical_data_storage_transaction.py`,
   26 tests) — see §14.

**ZIP reporting precision (per this correction's own finding about the
prior report):** a ZIP's **archive entries** (what `unzip -l` lists,
including directory entries) are distinct from the **actual files**
inside it. This build's `olive-phase3.2.zip` is reported in the
completion report using all three figures explicitly (total archive
entries, actual files, explicit directory entries) rather than
collapsing them into a single "N files" count.

No Phase 4 scope was introduced in this corrective pass either, and no
commit or push was made from this workspace — see the completion
report's "Git Status" section. No external approval of this build is
claimed anywhere in this document or the completion report.

## 18. Phase 3.3 correction summary

A third independent review, of the actual delivered `olive-phase3.2.zip`
(1009 tests passing, 1 skipped, `python main.py` successful, correct
ZIP root layout, every headline Phase 3.2 fix confirmed present),
found further adversarially-discovered canonical-storage integrity
gaps, all fixed in this corrective pass with regression tests. In
order of severity:

1. **Pre-existing identical duplicates could crash a successful write
   AFTER it had already committed (critical).** `existing_count` was
   computed from a partition's RAW (pre-dedup) row count, so
   re-submitting a bar already stored as two identical copies computed
   `len(merged) - existing_count == -1`, which
   `HistoricalWriteResult.__post_init__` rejected only after the
   partition's new data was already durably written to disk — fixed
   by canonicalizing existing bars before computing any quantity that
   feeds the result object's construction (§2/§3).
2. **A misplaced partition (wrong calendar month) could pass every
   existing check and be silently filtered to an innocent-looking
   empty result (critical).** A whole valid, checksum-matching
   parquet+manifest pair copied from one month's directory into
   another's passed every prior identity/checksum check; the misplaced
   bar was merely filtered out by the half-open-range filter at the
   end of `read_bars`. Fixed with two independent layers: the
   manifest's own new `partition_year`/`partition_month` fields
   checked against the directory, AND a per-bar content check
   independent of what the manifest claims — verified via an
   adversarial manual reproduction where the manifest was forged to
   correctly claim the right directory while the actual bar content
   was still the wrong month (§6/§7).
3. **One partition could hold internally incoherent bars.** The
   write-side grouping key never covered `tick_size`/
   `provider_raw_symbol`/`provider_instrument_id`/`data_label`/
   `schema_version` — two individually well-formed bars with different
   economics/provenance could land in, and later be read back from,
   the same partition. Fixed with an explicit coherence check enforced
   on every write and every read (§9/§10).
4. **`read_bars` did not reject an orphan manifest (manifest present,
   parquet missing)** — silently treated as "nothing was ever written"
   instead of the corrupt/orphaned state it actually is. Fixed;
   symmetric with the existing orphan-parquet rejection (§5).
5. **`read_bars` did not validate duplicate canonical keys within a
   partition's own content**, only `write_bars` did. Fixed:
   `_canonicalize_bars` is now the single shared policy (identical →
   canonicalize to one; conflicting → raise) applied by both `write_bars`
   and `read_bars`, including as defense in depth on `read_bars`'s
   final cross-partition result (§4/§15).
6. **A manifest's rich descriptive fields (`record_count`, first/last
   timestamp, `provider_raw_symbol`, `tick_size`, ...) were never
   cross-checked against what was actually decoded from its own
   partition** — only the raw-bytes checksum was ever verified. Fixed
   with `_verify_manifest_content_matches_bars`, run against raw
   (pre-canonicalization) decoded bars so a tolerated pre-existing
   identical duplicate is never mistaken for manifest/content
   disagreement (§8).
7. **Databento symbology instrument-ID resolution accepted malformed
   values through to a paid fetch.** A blank/whitespace/non-string/
   non-positive resolved instrument ID, or a malformed mapping entry
   shape, was not rejected until (at best) a later mismatch check —
   fixed with strict upfront validation of both entry shape and value
   format, raising `ProviderSymbologyError` before any paid
   `timeseries.get_range()` call (§11/§12).
8. **`HistoricalBar`'s tick-count/volume fields had no storage-
   representability upper bound** — a value `HistoricalBar` would
   accept could exceed what Olive's own Parquet int64 columns can
   represent. Fixed with `ARROW_INT64_MAX = 2**63 - 1`, enforced at
   construction (§13).
9. **`_bars_to_table`'s actual Arrow conversion calls had no exception
   translation** — an expected Arrow representation failure could leak
   a raw pyarrow/Arrow exception through this module's public boundary.
   Fixed, narrowly scoped to just the `pa.array`/`pa.Table.from_arrays`
   calls — the attribute-gathering loop above them is deliberately
   left unwrapped, so a genuine Olive bug there still propagates
   unmodified (§14).
10. Two further result-object invariants: `HistoricalWriteResult.
    partitions_written` can never exceed `total_records`; `HistoricalFetchResult`
    with status `SUCCESS` now requires a real non-negative
    `new_records_stored` (§16).
11. A new, entirely offline real-`databento`-package compatibility
    test (`tests/test_historical_data_databento_real_api.py`,
    `pytest.importorskip("databento")`, no HTTP request, no real API
    key) that introspects the installed package's documented interface
    (`Historical` constructor, `BentoError`/`BentoClientError`/
    `BentoServerError`, `DBNStore.to_df` parameters, and — via one
    narrowly-scoped real construction with a syntactically fake key —
    the `metadata`/`timeseries`/`symbology` namespaces) so installing
    the real package on the user's own machine proves this adapter's
    assumed interface still holds, independent of the fake-client
    behavioral tests (§17/§18/§19).
12. Permanent shipped regression coverage for every issue above,
    formalizing scenarios this correction's own manual adversarial
    verification scripts first reproduced
    (`tests/test_historical_data_storage_transaction.py`, 53 tests, up
    from 26 as of Phase 3.2) — see §14.

No Phase 4 scope was introduced in this corrective pass either, no
commit or push was made from this workspace — see the completion
report's "Git Status" section — and no external approval of this
build is claimed anywhere in this document or the completion report.

## 19. Phase 3.3 QA compliance rework (process correction, still Phase 3.3)

A fourth independent review of the actual delivered
`olive-phase3.3.zip` reproduced the Phase 3.3 baseline as genuinely
strong (1068 passing, 2 skipped, healthy `main.py`, correct layout, no
Phase 4 scope) but found that the Phase 3.3 completion process had
still declared every requirement satisfied without every standing QA
boundary actually having been exhaustively, adversarially re-tested.
This is NOT Phase 3.4 — it is a correction to the Phase 3.3 delivery
and to the completion PROCESS itself. Four confirmed defects, all
fixed in this corrective pass with regression tests:

1. **Manifest integer fields were not strict against Python's bool/
   float equality traps.** `True == 1`, `True in {1}`, and `1.0 == 1`
   are all `True` in Python, so a tampered manifest with
   `schema_version=True`, `record_count=True`, or `contract_year=
   2026.0` passed every existing `!=`/`not in` check undetected. Fixed
   with a single reusable manifest-schema-validation boundary
   (`_require_valid_manifest_schema`) that explicitly type-checks
   every durable manifest field — rejecting `bool` before accepting
   `int`, requiring Decimal/timestamp fields to be the exact string
   serialization Olive always writes, and enforcing the cross-field
   invariant `actual_first_timestamp_utc <= actual_last_timestamp_utc`
   — called immediately after a manifest is parsed as JSON, before any
   downstream comparison.
2. **`record_count == 0` was not rejected as the impossible/corrupt
   state it actually is.** `write_bars` never creates a partition
   directory without at least one bar, so a persisted manifest
   claiming zero records is corrupt, not a legitimate edge case. Fixed
   as part of the same schema-validation boundary.
3. **Malformed nested Databento symbology containers leaked a raw
   `TypeError` instead of the documented `ProviderSymbologyError`.**
   `{"result": {"NQZ6": 123}, "not_found": [], "partial": []}` reached
   `for entry in entries:` with `entries=123`; `not_found=123` and
   `partial=123` both reached `"NQZ6" in 123` — in both cases because
   `mapping.get(field) or []` only replaces a FALSY value, so a
   wrongly-typed but TRUTHY value (an `int`) passed straight through.
   Fixed by removing that coercion-by-truthiness pattern entirely and
   adding explicit shape validators for `not_found`/`partial`
   (`_require_symbology_container`), `result`
   (`_require_symbology_result_mapping`), and
   `result[raw_symbol]` (`_require_symbology_entries_sequence`), each
   raising `ProviderSymbologyError` before any `in`/iteration touches
   the value — validating the WHOLE response tree, not just leaf `"s"`
   values (which Phase 3.3 §11/§12 already covered).
4. **Two result-object impossible states were still constructible.**
   `HistoricalReadResult(bars=(real_bar,), partitions_found=0)` and
   `HistoricalWriteResult(partitions_written=0, new_records=0,
   total_records=5)` both succeeded despite being impossible outcomes
   of `read_bars`/`write_bars`. Fixed with one new cross-field
   invariant on each (`bars and partitions_found < 1` /
   `total_records > 0 and partitions_written == 0`), derived to be
   sufficient without over-constraining the legitimate idempotent-
   duplicate-write case.

74 new regression tests were added (46 in
`tests/test_historical_data_storage_transaction.py`, now 99, and 26 in
`tests/test_historical_data_databento.py`, now 125), preserving the
full 1068/2-skipped baseline — total suite: 1142 passed, 2 skipped. See
`CLAUDE.md`'s "Nested-boundary adversarial testing and completion-claim
lessons" section for the durable process lesson this rework produced:
a completion report never claims "all requirements satisfied" without
an evidence-backed compliance ledger with no unverified item. No Phase
3.4 or Phase 4 scope was introduced, and no external approval of this
build is claimed anywhere in this document or the completion report.

### §20 — Phase 3 final completion pass

A fifth review, this time of the actual delivered Phase 3.3 QA-rework
ZIP, found four remaining gaps, all one level deeper than anything
tested before — each a case where a CONTAINER or a REQUESTED slice was
validated but something nested inside, or beside, it was not:

1. **Manifest `last_written_at_utc` was written every time but never
   validated on read.** It is now required to be a non-null,
   well-formed, UTC-aware ISO timestamp via the same reusable
   `_require_manifest_utc_timestamp` validator every other manifest
   timestamp field already used — no duplicate logic.
2. **Manifest `requested_start_utc`/`requested_end_utc` were each
   individually validated but never compared to each other.** Both
   fields' parsed values are now captured and, when both are
   non-null, checked for `requested_start_utc < requested_end_utc`
   (strict); `start == end` and `start > end` both raise
   `HistoricalStorageIntegrityError`. A one-sided or fully-absent pair
   remains a legitimate manifest state and is still accepted.
3. **Databento `not_found`/`partial` were validated as containers
   (list/tuple) but never validated member-by-member.**
   `not_found=[123]` or `partial=[{"symbol": "NQZ6"}]` previously
   passed the container check, then silently failed the `raw_symbol
   in not_found` membership test (a string never equals an int or a
   dict) — functionally indistinguishable from the symbol genuinely
   not being flagged. `_require_symbology_container` now requires
   every element to be an actual non-empty string, raising
   `ProviderSymbologyError` before any membership test runs.
4. **The `result` mapping was only ever checked at the requested raw
   symbol's own slice.** A sibling key's entries (wrong container
   type, or a malformed `"s"` value) could be malformed without
   affecting whether `result[raw_symbol]` itself resolved cleanly —
   accepting a structurally untrustworthy whole response as long as
   the one slice Olive happened to read was fine. A new
   `_require_valid_symbology_result` validates every key, every
   value's container shape, and every entry's `"s"` value across the
   ENTIRE `result` mapping before any key is read.

`d0`/`d1` (the symbology entry's resolved-interval bounds) were
reviewed and deliberately left unvalidated: no Olive code path ever
reads either field, so no malformation of them can affect correctness.
The two properties `d0`/`d1` would otherwise exist to protect — no
identity ambiguity within the requested window, and full coverage of
that window — are already independently guaranteed by the existing
`len(distinct_ids) != 1` check and by Databento's own `not_found`/
`partial` classification, respectively. Adding validation for a field
nothing consumes would be complexity with no correctness benefit.

A full audit of every `isinstance(value, int)` comparison and every
`value or default` truthiness-coercion pattern across `app/data` and
`app/futures` found no further live occurrences beyond what Phase 3.3
already fixed (one apparent `isinstance(value, int)` hit in
`app/futures/models.py` was confirmed, on inspection, to already
exclude `bool` correctly).

42 new regression tests were added across
`tests/test_historical_data_storage_transaction.py` (+17, now 118),
`tests/test_historical_data_databento.py` (+23, now 148),
`tests/test_historical_data_provider_base.py` (+1), and
`tests/test_historical_data_models.py` (+1) — preserving the full
1142/2-skipped baseline: total suite now 1184 passed, 2 skipped. See
`CLAUDE.md`'s "Member-level and sibling-entry validation lessons"
section for the durable process lesson this pass produced: a validated
container's TYPE says nothing about its members' types, and a
validated requested-key slice says nothing about its siblings in the
same response — both must be checked explicitly, not inferred. No
Phase 3.4 or Phase 4 scope was introduced, and no external approval of
this build is claimed anywhere in this document or the completion
report.
