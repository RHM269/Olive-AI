# Olive AI — Real-Time Market Data (Phase 4, Phase 4.2 real-package hardening applied)

This document describes Olive's real-time market-data subsystem as it
stands today: what it does, what it deliberately does not do, its
safety model, its reconnect/staleness behavior, and its known
limitations. It assumes familiarity with `docs/architecture.md`,
`docs/futures_domain.md` (Phase 1-2), and `docs/historical_data.md`
(Phase 3).

## 1. Scope

Phase 4 adds the ability to open a live streaming connection to a
configured provider for Olive's exact production tradable universe
(`NQ`, `MNQ`), normalize vendor-specific trade/quote/bar records into
Olive's own domain events, and report connection/liveness state
honestly, strictly on explicit opt-in. It adds:

- A provider-independent real-time-data interface
  (`app/data/live_provider_base.py`) and two implementations: a safe
  default (`UnconfiguredLiveProvider`) and a real Databento live
  adapter (`DatabentoLiveProvider`).
- Typed, self-validating subscription/event/status domain objects
  (`app/data/live_models.py`).
- `RealTimeDataService` (`app/data/live_service.py`), which gates every
  stream open through Olive's production-domain check, subscription
  validation, provider configuration, and an explicit network opt-in
  -- in that order -- before any provider connection is attempted, and
  adds session-aware staleness disambiguation on top of the provider's
  own liveness telemetry.
- `build_live_provider` (`app/data/providers/live_factory.py`), which
  never performs I/O and never imports the real vendor client object.
- A real `Real-time market data` health check (`app/health.py`).

## 2. Explicitly out of scope (deferred to later phases)

Phase 4 does **not** implement, and this build contains no code path
for: persisting live events to disk or to the Phase 3 historical
Parquet store; back-adjusted/continuous price series; order-book
reconstruction beyond top-of-book (MBP-1) quotes; feature engineering
(moving averages, RSI, or any derived indicator); strategies,
predictions, or signals; backtesting or market replay; paper trading;
alerting; any tradable scope beyond NQ/MNQ; or any UI. `Feature engine`
and every later-phase subsystem continue to report `NOT_IMPLEMENTED` in
the health report -- Phase 4 touches nothing about them.

## 3. Relationship to Phase 3 (a sibling subsystem, not an extension)

Phase 4 is deliberately **not** built as an extension of Phase 3's
historical-data subsystem, even though both ultimately describe NQ/MNQ
market activity:

- **Separate error hierarchy.** `app.data.live_models.LiveDataError` is
  its own root, a sibling to `app.data.models.HistoricalDataError`, not
  a subclass of it.
- **Separate domain objects.** `LiveTrade`/`LiveQuote`/`LiveBar` are not
  `HistoricalBar` subclasses or variants -- they model fundamentally
  different things (a live point-in-time event stream, not a durable
  OHLCV bar record) and are never written to `HistoricalBarStore`.
  `LiveBar` does reuse `app.data.models.HistoricalTimeframe.ONE_SECOND`
  as its interval type, since "one second" is a timeframe concept
  genuinely shared by both subsystems, not a coincidence worth
  duplicating.
- **Separate provider interface, separate adapter.** `RealTimeMarketDataProvider`
  is a new ABC, not a variant of `HistoricalMarketDataProvider`,
  because connecting and streaming is a fundamentally different
  lifecycle from a one-shot fetch (open once, consume a long-lived
  iterator, disconnect/reconnect, close). Only
  `app.data.providers.databento_live` knows Databento's *live* API
  shape (the `Live` client, its record types); it shares no code with
  `app.data.providers.databento`, Phase 3's *historical* adapter,
  beyond the same secret-safe exception-translation discipline.
- **Separate configuration, separate network kill switch.** Every
  Phase 4 setting is independently named (`OLIVE_LIVE_*`) and
  `OLIVE_LIVE_NETWORK_ENABLED` is checked independently of
  `OLIVE_HISTORICAL_NETWORK_ENABLED` -- enabling one never enables the
  other (regression-tested explicitly; see §9).
- **What genuinely is shared, and only this:** Olive's futures domain
  (`app.futures.models`/`registry`/`validation`/`sessions`) and the
  `DATABENTO_API_KEY` credential (one credential, reused by whichever
  of the two subsystems is configured to use the same vendor). Neither
  subsystem duplicates a canonical NQ/MNQ fact or a tradability rule --
  both delegate to Phase 2's/Phase 3's existing validators.

## 4. Supported providers, event types, and schemas

`LiveProviderKind`: `UNCONFIGURED` (the only safe default) or
`DATABENTO`. No other real-time vendor is implemented in this build.

`LiveEventType`: `TRADE`, `QUOTE`, `BAR`, mapped explicitly to
Databento's live schemas via `LiveEventType.databento_schema`:
`TRADE` -> `trades`, `QUOTE` -> `mbp-1` (top-of-book, not full
order-book depth), `BAR` -> `ohlcv-1s` (one-second bars only -- Olive
never requests a live OHLCV schema coarser than one second; any
coarser aggregation is deferred to a later phase). No other Databento
live schema (MBO, MBP-10, definition, statistics, imbalance, ...) is
used anywhere in this codebase.

Exactly like Phase 3, Olive always subscribes using Databento's
`stype_in="raw_symbol"` with each subscribed contract's own CME-style
display code (`FuturesContract.display_code`, e.g. `NQZ6`) -- derived
internally from Olive's Phase 2 `FuturesContract`, never accepted as a
raw string from a caller, and never Databento's parent (`NQ.FUT`) or
continuous (`NQ.c.0`) symbology.

## 5. Domain model

- `LiveSubscriptionRequest` (immutable): `contracts` (a non-empty tuple
  of real `FuturesContract` instances, each one of Olive's required
  NQ/MNQ roots, no duplicates), `event_types` (a non-empty tuple of
  real `LiveEventType` values, no duplicates). Does **not** itself
  check production-economics correctness (tick size, tick value, ...)
  of a contract's instrument -- that is `RealTimeDataService`'s job
  (§9), exactly mirroring Phase 3's own split (§7 below).
- `ConnectionState`: `DISCONNECTED`, `CONNECTING`, `CONNECTED`,
  `RECONNECTING` (a transient failure is being retried -- the
  underlying client connection is actively being torn down and
  rebuilt), `STOPPED` (deliberately closed), `FAILED` (a permanent
  failure, e.g. authentication, that will never be retried), and
  `DEGRADED`. **Phase 4 correction:** `DEGRADED` was originally
  declared but never produced by the shipped Databento adapter; it is
  now actually used -- a documented
  `SKIPPED_RECORDS_AFTER_SLOW_READING` `ErrorMsg` (real, non-session-
  fatal market-data loss) transitions the stream into `DEGRADED` and
  increments `LiveStreamStatus.data_gap_count`, clearing back to
  `CONNECTED` on the next successfully accepted event (proving the
  stream recovered) -- see §11.
- `LiveTrade` / `LiveQuote` / `LiveBar` (immutable): Olive's canonical
  live event objects. Prices are exact `Decimal` values, never
  `float`, and are enforced tick-aligned to the subscribed instrument's
  own `tick_size` by the adapter before construction (§11). Each
  carries `contract` (the real `FuturesContract`, reused directly --
  not flattened to a root/year/month triple, since live events are not
  persisted this phase and have no storage-schema pressure to do so),
  three INDEPENDENTLY meaningful timestamps -- `ts_event` (the
  exchange/event time; a timezone-aware UTC `datetime`, naive or
  non-UTC rejected), `ts_recv` (Databento's own receive time, when the
  schema supplies one), and `olive_received_at` (Olive's own local
  wall-clock receive time, computed once per record in
  `_handle_record` and threaded through to every constructed event --
  **Phase 4 correction:** this field already existed on every event
  model but was computed and then silently discarded by the adapter;
  it is now actually populated) -- and `data_label`, which **must** be
  exactly `DataLabel.LIVE` (reusing Phase 3's
  `app.data.models.DataLabel` enum) -- there is no code path in Phase
  4 capable of constructing a live event labeled
  `HISTORICAL`/`DELAYED`/`SIMULATED`/`DEMO`, and every event model
  enforces this itself in `__post_init__`, regardless of which code
  path constructed it. `LiveQuote`'s bid/ask side is all-or-nothing: a
  `bid_price` without a `bid_size` (or the reverse) is rejected, but a
  one-sided quote (bid present, ask absent, or vice versa) is
  legitimate and distinct from a two-sided one.
- `LiveStreamStatus` (immutable): `state` (`ConnectionState`),
  `events_received`, `events_rejected` (malformed/unmapped records,
  counted rather than crashing the stream), `reconnect_count`, a
  `last_receive_at` timestamp, and `is_stale` (computed only while
  `state is ConnectionState.CONNECTED` -- a deliberately closed
  (`STOPPED`) or never-connected stream is a different, honestly
  distinguished state, never conflated with "stale"). **Phase 4
  correction** added two further counters, deliberately never
  cross-validated against each other (a reconnection can succeed on
  its very first attempt): `reconnect_attempts_total` (every
  connection attempt that failed, at initial connect *or* mid-stream
  reconnect -- `reconnect_count` itself now counts ONLY a successful
  RE-establishment of a previously-lost connection, never an
  initial-connect retry, however many attempts that took), and
  `data_gap_count` (a cumulative tally of `SKIPPED_RECORDS_AFTER_SLOW_
  READING` conditions encountered -- see `DEGRADED` above).

## 6. Error hierarchy

`LiveDataError` (new, separate root -- see §3) and its subclasses:
`InvalidLiveSubscriptionError`/`InvalidLiveServiceConfigurationError`
(malformed subscription/constructor arguments, or a contract that
fails Olive's production-tradability gate), `LiveProviderNotConfiguredError`,
`LiveNetworkDisabledError` (the network kill switch), and
`LiveProviderError` and its subclasses: `LiveProviderAuthenticationError`
(permanent, never retried), `LiveProviderPermissionError` (permanent,
never retried), `LiveReconnectExhaustedError` (every configured
transient-reconnect attempt failed), `LiveStreamClosedError` (an
operation attempted on a stream that is not in a state that supports
it -- e.g. `events()` before `connect()`, or a double `connect()`),
`LiveContractIdentityError` (a provider symbol-mapping record
contradictorily remaps an already-mapped instrument ID to a different
contract -- fails the stream closed rather than silently mixing two
contracts' data), `LiveIdentityUnresolvedError` (a
`LiveContractIdentityError` subclass, added by the Phase 4 correction
-- raised when Olive cannot authoritatively confirm a subscribed
contract's full-year identity before a live connection opens, or when
a live `SymbolMappingMsg` contradicts that pre-verified identity; see
§8), `LiveProviderDataError` (a malformed record field; also raised
when an MBP-1 quote record exposes neither of the two documented
top-of-book representations -- see §11), `LiveProviderShutdownError`
(added by the Phase 4.2 correction -- raised by `close()` when the
underlying client reports an unexpected, not-specifically-recognized
`databento`-origin exception while being stopped; see §11), and
`LiveTickMisalignedError` (a price that is not an exact integer
multiple of the production instrument's `tick_size`).

## 7. Generic structural validity vs. Olive production correctness

Exactly the same distinction Phase 2.4/Phase 3 established (see
`docs/futures_domain.md` §9, `docs/historical_data.md` §7, and
`CLAUDE.md`) applies here, unchanged: `LiveSubscriptionRequest` only
checks that its contracts are *structurally* one of Olive's required
roots (via `app.futures.validation.REQUIRED_TRADABLE_ROOTS` as an
allow-list) -- it does not re-verify that the loaded NQ/MNQ instrument
*definitions* are themselves Olive's canonical financial specification.
That deeper check is `RealTimeDataService.open_stream`'s job (§9),
which runs the full `validate_olive_futures_domain` whole-domain gate
and the per-contract `require_olive_tradable_contract` gate -- the
exact same two Phase 2/Phase 3 validators Historical data already
uses, reused verbatim rather than re-implemented or approximated for
the live path.

## 8. Exact contract identity enforcement

Databento's raw futures symbols encode only the *last digit* of the
contract year (`NQZ6` is `NQ`'s December contract in 2026 **and**
2036, and every other decade ending in 6) -- a problem Phase 3's
historical adapter already solved for one-shot fetches via a
point-in-time `symbology.resolve` lookup before any paid call. The
*live* protocol has the identical ambiguity, but a Databento live
`SymbolMappingMsg` reports only the short raw symbol and an
`instrument_id` -- it never itself proves which full year/month that
`instrument_id` actually corresponds to. **The originally delivered
Phase 4 build did not address this at the live layer at all**: it
matched an incoming `SymbolMappingMsg` to the *first* subscribed
contract sharing that raw symbol, which both silently mislabeled a
single far-future contract and, for two contracts genuinely sharing a
raw symbol, could attribute live data to the wrong one. The Phase 4
correction pass fixes this architecturally, in two layers:

### 8.1 Full-year identity proof (before any live connection opens)

Before `connect()` ever subscribes to the live stream, `DatabentoLiveProvider`
authoritatively resolves **every** subscribed contract's full-year
identity via Databento's point-in-time `symbology.resolve` metadata
endpoint -- the same free, already-hardened mechanism (and the same
underlying parser, `_distinct_resolved_instrument_id`, imported
directly from Phase 3's historical adapter rather than reimplemented)
Phase 3 uses, now reused for a second, separate purpose. Each
contract's resolution is scoped to that contract's **own** calendar
month (`[year-month-01, next-month-01)`), never an arbitrary "today"-
anchored window: a contract that genuinely exists has an established
raw-symbol mapping covering its own month (CME lists quarterly futures
well ahead of their trading month); a contract that does not exist yet
(e.g. a decade-away request) has no such mapping and fails resolution
closed. Three outcomes, each raising `LiveIdentityUnresolvedError`
(and transitioning the provider straight to `FAILED`) rather than ever
opening a connection:

- **Not (yet) listed for its own month** -- Databento's own `not_found`
  classification for that exact window. This is what stops a single
  contract from being labeled with an arbitrary full year merely
  because its raw symbol happens to already mean something else today.
- **Ambiguous / partial / malformed resolution** -- anything
  `_distinct_resolved_instrument_id` itself rejects (see its own
  docstring in `app/data/providers/databento.py`) is translated into
  `LiveIdentityUnresolvedError` at this adapter's boundary.
- **Instrument-ID collision** -- two *different* subscribed contracts
  (by `FuturesContract.identity`, not by raw symbol) resolving to the
  *same* `instrument_id` is rejected outright, even though each was
  individually resolved against its own, correctly-scoped month. This
  is the direct fix for the audit's `NQ-2026-12` vs. `NQ-2036-12`
  example: requesting both together is rejected before any live
  connection opens, rather than silently accepted with duplicate raw
  symbols subscribed and a 50/50 "first match wins" resolution later.

The resulting `{contract.identity: instrument_id}` map
(`self._expected_instrument_ids`) is the authoritative ground truth
every live `SymbolMappingMsg` is checked against once streaming
begins -- not a replacement for runtime mapping, but its pre-verified
foundation. A metadata client missing the `symbology.resolve`
capability (or the real package being unavailable; see §14) raises
`LiveIdentityUnresolvedError` immediately, rather than accepting a live
connection with no way to prove identity.

### 8.2 Live-mapping enforcement (while streaming)

Databento's live `SymbolMappingMsg` records still tell a subscriber
which `instrument_id` a given raw symbol has been assigned for the
life of the session -- and remain the *only* way the adapter can tell
which subscribed contract a subsequent trade/quote/bar record (which
carries only an `instrument_id`, not a symbol) actually belongs to.
`DatabentoLiveProvider` builds its runtime `instrument_id ->
FuturesContract` map exclusively from mapping records whose
`stype_in_symbol` matches one of the *subscribed* contracts' own
`display_code` **and** whose reported `instrument_id` matches that
exact contract's own pre-verified id from §8.1 -- a mapping for a
symbol Olive never subscribed to is rejected (counted, not raised,
since an unrelated mapping arriving on the wire is not itself a
protocol violation); a mapping that contradicts every subscribed
candidate contract's own pre-verified identity raises
`LiveIdentityUnresolvedError` rather than ever being trusted. Market-
data records for an `instrument_id` with no recorded mapping yet are
rejected (counted) -- Olive never guesses which contract an unmapped
record belongs to.

A **contradictory remapping** -- the same `instrument_id` later mapped
to a *different* contract than it was already mapped to -- raises
`LiveContractIdentityError` and stops the stream. (With §8.1's
collision check in place, this specific internal check is no longer
reachable through the public API under normal operation -- two
contracts can no longer share a pre-verified `instrument_id` in the
first place -- but it remains as defense-in-depth against a bug in
§8.1 itself, and is exercised directly against the underlying method
in `tests/test_live_data_databento.py`.) This is deliberately
fail-closed: silently accepting the new mapping could mean Olive has
already attributed some number of trades/quotes/bars to the wrong
contract before noticing, and there is no way to safely "undo" events
already yielded to the caller. A benign re-mapping of an
`instrument_id` to the *same* contract it was already mapped to is not
an error (Databento may legitimately re-send a mapping record).

## 9. `RealTimeDataService` -- the gate pipeline

`RealTimeDataService.open_stream(subscription)` runs, in this exact
order, raising a specific `LiveDataError` subclass at the first gate
that fails:

1. **Subscription validation** -- `subscription` must be a real
   `LiveSubscriptionRequest`; malformed input raises
   `InvalidLiveSubscriptionError`.
2. **Olive whole-domain production gate** -- the whole futures domain
   must itself be production-valid
   (`validate_olive_futures_domain`); otherwise
   `InvalidLiveServiceConfigurationError`. **No provider method is
   ever called before this gate passes.**
3. **Per-contract production-tradability gate** -- every subscribed
   contract must individually pass `require_olive_tradable_contract`
   (reused from Phase 3's `app.data.validation`); otherwise
   `InvalidLiveSubscriptionError`.
4. **Provider configuration** -- if the configured provider is
   `UnconfiguredLiveProvider`, raises `LiveProviderNotConfiguredError`
   naming the exact reason (no provider selected; selected but no API
   key; selected but package not installed).
5. **Network opt-in** -- if `OLIVE_LIVE_NETWORK_ENABLED` is not
   `true`, raises `LiveNetworkDisabledError` **before calling the
   provider's `connect()` at all.**
6. **Provider connect** -- calls `provider.connect(subscription,
   instruments)`, passing the production-validated
   `FuturesInstrument` map (not merely the subscribed `FuturesContract`
   objects) so the adapter can enforce tick alignment (§11) against
   Olive's own validated economics, never the provider's own. A
   connect failure propagates as whatever `LiveProviderError` subclass
   the adapter raised.

`close_stream()`, the `status` property, and the `provider` property
are thin passthroughs to the underlying provider. `is_feed_unexpectedly_stale`
is covered in §12.

**The network kill switch is enforced solely at this layer, never
inside `DatabentoLiveProvider` itself** -- exactly mirroring how
Phase 3's `historical_network_enabled` is enforced solely in
`HistoricalDataService`, not its adapter. `RealTimeDataService` is the
only path that can reach `provider.connect()`; the health check (§13)
deliberately never does.

## 10. Configuration

| Variable | Default | Meaning |
|---|---|---|
| `OLIVE_LIVE_PROVIDER` | `unconfigured` | `unconfigured` or `databento`. |
| `DATABENTO_API_KEY` | *(empty)* | Reused from Phase 3 -- one credential, shared by whichever subsystem is configured to use it. Never logged, echoed, or included in any error/health output. |
| `OLIVE_LIVE_NETWORK_ENABLED` | `false` | Master kill switch; must be explicitly `true`. Independent of `OLIVE_HISTORICAL_NETWORK_ENABLED`. |
| `OLIVE_LIVE_STALE_THRESHOLD_SECONDS` | `10` | Seconds since the last received event before the stream is considered stale (subject to session-awareness; see §12). Must be strictly positive. |
| `OLIVE_LIVE_RECONNECT_MAX_ATTEMPTS` | `5` | Maximum consecutive reconnect attempts after a transient failure. Permanent failures (auth/permission) are never retried regardless of this setting. |
| `OLIVE_LIVE_RECONNECT_BASE_DELAY_SECONDS` | `1` | Bounded-exponential-backoff base delay in seconds: attempt *N*'s delay is `base_delay * 2^(N-1)`, capped at `max_delay`. |
| `OLIVE_LIVE_RECONNECT_MAX_DELAY_SECONDS` | `30` | Backoff cap in seconds. Must be `>= base_delay`. |

All seven defaults are fail-closed: loading settings, importing any
module in this subsystem, running the test suite, running `main.py`,
or constructing a `RealTimeDataService`/provider can never, by itself,
open a live connection. Every field is validated and canonicalized in
`Settings.__post_init__` regardless of construction path, following
the exact Phase 3.1 discipline (`live_provider` must be a real
`LiveProviderKind`; `live_network_enabled` must be an actual `bool`,
never a truthy string; the three `Decimal` fields must be finite and
positive, with `max_delay_seconds >= base_delay_seconds` cross-checked;
`live_reconnect_max_attempts` must be a true, non-bool `int`).
`Settings.live_config_summary()` is the sanctioned way to describe live
configuration in status output, and never includes the key itself.

## 11. Databento live adapter

`DatabentoLiveProvider` is the only module in this codebase that knows
Databento's *live* API shape. It never imports the real `databento`
package at module scope or at `Settings`/service-construction time --
`build_live_provider` performs a lazy `import databento` only when
`OLIVE_LIVE_PROVIDER=databento` and a key is present, purely to confirm
the package is installed, and never constructs a real client merely to
check that. `DatabentoLiveProvider.__init__` itself accepts **two**
injectable factories, neither ever called during `__init__` -- only
`connect()` (and an internal reconnect loop) ever calls them:

- `client_factory: Callable[[], Any]` -- builds the streaming `Live`
  client. A Databento `Live` client typically cannot be reused after a
  connection error, so each (re)connection attempt gets a fresh client
  instance via a fresh factory call.
- `metadata_client_factory: Callable[[], Any]` -- builds a separate
  `Historical` client used ONLY for the full-year identity proof
  (§8.1), called once per `connect()`, before the streaming client is
  ever built. This is a second, distinct client because
  `symbology.resolve` is documented as a `databento.Historical`
  capability, not something the streaming `Live` client itself exposes.

**Corrected Databento 0.87 Live API usage (Phase 4 correction).** The
originally delivered adapter constructed `databento.Live(key=...,
dataset="GLBX.MDP3")` and then called `client.start()` before
synchronously iterating -- both wrong against the documented 0.87 API:
`Live(key=...)` takes NO `dataset` parameter at all (dataset belongs
only in `client.subscribe(dataset=..., ...)`), and `Live.__iter__()`
is documented to auto-start the session itself for synchronous
consumption -- calling `.start()` first and then iterating is
documented to raise `ValueError`. The corrected adapter constructs
`databento.Live(key=...)` with no `dataset` kwarg, never calls
`.start()` anywhere, and consumes purely via `for record in client:`.
`_require_databento_live_client_interface` reflects this: it requires
only `subscribe`/`stop`/`__iter__`, deliberately NOT `start` (requiring
it would accept a client this adapter does not actually need, and
could mask the opposite mistake -- a client that only supports the
callback/`start()` model, which this adapter's architecture never
uses). None of this could be verified against the real installed
package in this sandbox (see §14); the compatibility fakes in
`tests/test_live_data_databento.py` deliberately ENFORCE this exact
signature/lifecycle (the fake `Live` constructor accepts only `key`;
the fake client's `start()` raises `AssertionError` if ever called) so
a future regression back to the old, incorrect shape fails every
single test in that file immediately, rather than being silently
tolerated by a permissive fake.

**Record classification is duck-typed, not `isinstance`-based.** A
record is classified by its attribute shape (`price`/`size` ->
`TRADE`; `bid_px`/`ask_px`/... -> `QUOTE`; `open`/`high`/`low`/`close`
-> `BAR`; `stype_in_symbol`/`stype_out_symbol` -> symbol mapping;
`msg`/`code` with no price field -> system message; `err`/`is_last` ->
error message), never by checking the record's type against the real
`databento` package's own classes. This is a deliberate choice: it
lets the exact same classification code be exercised by tests using
plain fakes, without the real package installed, and it behaves
identically whether or not the real package happens to be present.

**Prices are fixed-point integers, converted exactly.** Databento
encodes live prices as `int64` scaled by `1e9`; the adapter converts
via `Decimal(raw) / Decimal("1000000000")`, never a float division,
and then enforces **tick alignment** against the subscribed
instrument's own `tick_size` (`price % tick_size == 0`) for every
trade price, every present quote bid/ask price, and every bar OHLC
value -- a misaligned price raises `LiveTickMisalignedError`, counted
as a rejected event rather than crashing the whole stream (field
computation for a record happens inside a narrow `try`/`except
LiveDataError` so a malformed field rejects just that one record).

**MBP-1 top-of-book extraction (Phase 4.2 correction §1).** The
Phase 4 correction's own quote branch still gated `levels[0]` on
`isinstance(levels, (list, tuple))` and silently fell back to reading
`bid_px`/`ask_px`/`bid_sz`/`ask_sz` directly off the *whole* `Mbp1Msg`
record -- fields that do not exist there -- whenever that check
failed. Every test double used a plain Python `list` for `levels`, so
this defect went undetected until independent audit flagged it: a real
Databento `Mbp1Msg.levels` array's element type (`BidAskPair`) is
documented as an indexable provider structure, not necessarily a
built-in `list`/`tuple`, and Databento 0.87 additionally exposes the
same level-0 data via flat `bid_px_00`/`ask_px_00`/`bid_sz_00`/
`ask_sz_00` properties directly on the record. `_mbp1_top_of_book_fields`
now: (1) prefers the flat `*_00` properties whenever the record exposes
ANY of them; (2) otherwise treats `levels` as genuinely indexable --
`levels[0]` inside a narrow `try`/`except (TypeError, IndexError,
KeyError)`, never an `isinstance` gate against built-in container
types; (3) raises `LiveProviderDataError` -- never a silently empty
quote -- if neither representation yields a usable top-of-book object
(one exposing at least one of `bid_px`/`ask_px`/`bid_sz`/`ask_sz`).
`tests/test_live_data_databento.py` exercises this with a custom
`_IndexableLevels` container that is deliberately NOT a `list`/`tuple`
subclass, so a regression back to the `isinstance` check fails loudly
even though every other test in the file still uses a plain list.
`_classify_record` was updated in step: a record exposing only the
flat `*_00` properties (no `levels` attribute at all) is still
classified as a `QUOTE`, not `unknown`.

**A quote's side-presence is gated on size, not a price sentinel.**
Databento's actual "no resting order on this side" wire convention
could not be verified against the real package in this sandbox (see
§14); rather than guess at an unverified price-sentinel convention,
the adapter treats a side as present only when its **size** is
greater than zero -- size `0` unambiguously means "no order," whatever
the accompanying price field happens to contain. This is a documented,
deliberate choice, flagged here for verification against the real
package on a machine where it installs.

**Reconnection is bounded-exponential-backoff, injectable, and
permanent-failure-aware.** `ReconnectPolicy(max_attempts,
base_delay_seconds, max_delay_seconds)` computes
`delay_for_attempt(attempt) = min(base_delay * 2**(attempt-1),
max_delay)`; actual sleeping goes through an injectable `sleep_fn`
(default `time.sleep`), so every regression test for backoff timing
runs with zero real sleep. A connection failure is classified, by the
vendor exception's own status/message (never its message text --
see below), as either **permanent** (authentication, permission,
API-key deactivation, connection-limit-exceeded, invalid-subscription
-- raised immediately, `state=FAILED`, never retried) or **transient**
(retried up to `max_attempts` times, raising
`LiveReconnectExhaustedError` if every attempt fails). A transient
failure encountered **mid-stream** (inside `events()`, not just at
initial `connect()`) triggers the identical reconnect logic and
resumes yielding events from the newly reconnected client.

**Old-client cleanup during mid-stream reconnect (Phase 4.2 correction
§4).** The Phase 4 correction's own mid-stream recovery path replaced
`self._client` with a freshly constructed client immediately on an
iterator failure, without ever attempting to stop the OLD, now-broken
client first -- an iterator exception does not guarantee every
underlying resource/connection on that old client had already been
released. `_best_effort_stop_old_client` is now called with the old
client BEFORE a new one is established, using semantics deliberately
*more permissive* than `close()`'s own (§ below): ANY `databento`-
origin exception raised while stopping the old, already-broken,
about-to-be-discarded client -- recognized-benign or not -- is
absorbed, since forcing a hard failure out of best-effort cleanup here
would turn one already-observed transient failure into an unrelated
second one; a non-`databento`-origin exception (a genuine programming
bug) still always propagates, exactly as it would from `close()`.
`self._client` is reassigned to the new client, and `reconnect_count`
incremented, only once a genuinely new client has been *successfully*
established -- never before, and the old client is never left
referenced as the active client afterward.

**Truthful reconnect telemetry (Phase 4 correction).**
`LiveStreamStatus.reconnect_count`'s own documentation always promised
"successful reconnections only" -- but the originally delivered
implementation also incremented it for every failed attempt
encountered while establishing the very first (initial) connection,
counting a failed attempt as though it were a successful
re-establishment. The corrected adapter tallies these as two
genuinely distinct things: `reconnect_attempts_total` increments for
every failed connection attempt, whether during the initial connect or
a later reconnect (tallied inside `_connect_with_reconnect`, the
shared helper both paths call); `reconnect_count` increments by
exactly 1, and ONLY, in `events()`'s own mid-stream recovery path --
the one place a *previously successful* connection was actually lost
and then re-established -- regardless of how many internal attempts
that recovery needed (those attempts are tallied into
`reconnect_attempts_total` instead). An initial connection that took
several attempts to succeed therefore reports `reconnect_count == 0`
(no prior connection was ever lost) and `reconnect_attempts_total`
equal to however many attempts failed first.

**Secret safety mirrors Phase 3's adapter exactly** (see
`CLAUDE.md`'s vendor-integration lessons): a vendor exception's own
message text never reaches anything this adapter raises, logs, or
returns -- it is classified internally by status code/message
substring to pick a category, and only a fixed, pre-written, generic
message for that category is ever raised, with the exception chain
severed (`from None`) so even a full traceback print cannot surface
the original (including the API key itself, which Databento's own
documented auth-failure error can embed in its message).

**Control and informational records never crash the stream.**
`SystemMsg` records are counted (`events_received` increments) but
produce no `LiveEvent`. `ErrorMsg` records are handled by documented
`code`, in three tiers:

- **Fatal** (`_FATAL_ERROR_CODE_NAMES`): `AUTH_FAILED`,
  `API_KEY_DEACTIVATED`, `CONNECTION_LIMIT_EXCEEDED`,
  `INVALID_SUBSCRIPTION`, `INTERNAL_ERROR`, `REPLAY_DATA_AGED_OUT` --
  translates to the matching `LiveProviderError` subclass, sets
  `state=FAILED`, and stops the stream. **Phase 4 correction:**
  `INTERNAL_ERROR` and `REPLAY_DATA_AGED_OUT` were missing from the
  originally delivered fatal-code list entirely.
- **Data-gap, non-session-fatal** (`_DATA_GAP_ERROR_CODE_NAMES`):
  `SKIPPED_RECORDS_AFTER_SLOW_READING` -- documented as not closing
  the connection, but representing genuine, real market-data loss.
  **Phase 4 correction:** this condition previously fell through to
  the generic "any other non-fatal code" branch and was silently
  absorbed as though nothing had happened. It now increments
  `LiveStreamStatus.data_gap_count` and transitions `state` to
  `DEGRADED` (clearing back to `CONNECTED` on the next successfully
  accepted event) -- Olive must never continue as though a stream that
  just lost data stayed fully healthy, but the connection itself is
  still good, so raising would be the wrong response too.
- **Any other non-fatal code** (including `SYMBOL_RESOLUTION_FAILED`
  and any code this adapter does not specifically recognize by name):
  counted as a rejected event; streaming continues unaffected, which
  is Databento's own documented behavior for an unrecognized code.

**`close()` is idempotent and narrowly scoped, never a blanket
swallow, and no longer suppresses every vendor-origin exception
indiscriminately (Phase 4 correction, further narrowed by Phase 4.2
correction §3).** The originally delivered `close()` used `except
Exception: pass` around `client.stop()`, which could hide a genuine
programming bug (wrong method name, bad call signature) exactly as
easily as a benign, already-expected vendor-side shutdown condition.
The Phase 4 correction narrowed this to any exception merely
*originating from* the `databento` package -- better, but independent
audit flagged that this was still too broad: a vendor-origin exception
is not automatically a harmless one. `close()` now splits into three
cases: (1) a `databento`-origin exception whose message matches a
narrow, specifically recognized benign shutdown condition (via
`_is_benign_shutdown_exception` -- e.g. "already stopped," "connection
already closed," "not connected," and similar documented-benign
substrings) is suppressed; (2) a `databento`-origin exception that
does **not** match one of those conditions is an unexpected vendor
failure -- never silently discarded, it is translated into a
sanitized `LiveProviderShutdownError` and raised (secret-safe: the
vendor exception's own message text never reaches it, and the
exception chain is severed via `from None`); (3) a non-`databento`-
origin exception (a genuine programming bug) always propagates
unchanged, exactly as before. A `finally` clause guarantees the
provider still reaches a coherent `STOPPED` state on every one of
these three paths -- shutdown remains idempotent and safe without ever
hiding either an unexpected bug or an unexpected vendor failure.

## 12. Liveness and staleness

`LiveStreamStatus.is_stale` is the provider's own, session-*unaware*
signal: `last_receive_at` older than `stale_threshold_seconds` ago,
computed only while `state is ConnectionState.CONNECTED`. A
deliberately closed (`STOPPED`) stream is never "stale" -- it was
closed on purpose, not silently starved.

`RealTimeDataService.is_feed_unexpectedly_stale(at=None)` adds
session-awareness on top of that raw signal, reusing
`app.futures.sessions.session_state_at` (Phase 2) unchanged: if the
provider itself does not report `is_stale`, the feed is not stale,
full stop. If the provider *does* report `is_stale`, the service
checks the market session at the time in question (current time if
`at` is omitted) and reports `False` -- not stale, merely closed -- if
the session is `MAINTENANCE` or `WEEKEND_CLOSED`; only during an
actually-open session does a provider-reported stale signal become an
"unexpectedly" stale verdict. This prevents a legitimate daily
maintenance window or weekend closure from ever being misreported as
a live-feed outage.

## 13. Health reporting

`Real-time market data` in `python main.py`'s status report (and
`app.health.get_system_health`) is a real check, not a hard-coded
string, mirroring Phase 3's `Historical market data` check exactly:

- `NOT_CONFIGURED` -- no provider selected (the default), or a
  selected provider is missing required configuration (no API key, or
  the `databento` package is not installed) -- the exact reason is
  named.
- `CONFIGURED` -- a real provider was actually constructed (provider
  selected, API key present, vendor package importable); the detail
  notes the dataset, whether live network access is enabled or
  disabled, and the configured stale threshold, but never the key
  itself.
- `ERROR` -- configuration is present but broken (e.g. an API key that
  is only whitespace, which `DatabentoLiveProvider` itself rejects as
  empty even though `Settings.has_databento_api_key`'s truthiness
  check alone would have missed it), or `build_live_provider` itself
  raising a `LiveDataError`.

This check **never** calls `connect()`/`events()`/`close()` on the
resulting provider -- verified in `tests/test_live_data_health.py` by
injecting a client whose connection-relevant methods raise
`AssertionError` if ever touched, including in the `CONFIGURED` case
and even when `OLIVE_LIVE_NETWORK_ENABLED=true`.

## 14. Known limitations of this build

### Local Mac verification — 2026-10-08

The original sandbox limitations below describe artifact-build history.
Local Phase 4.2 verification supersedes the package-availability and
record-shape limitations for Python 3.11.16, databento 0.87.0,
databento-dbn 0.70.0, pyarrow 25.0.1, pandas 3.0.6, and pytest 8.4.2.
The targeted compatibility suite passed 184 tests; the complete suite
passed 1748 tests, with zero failures and zero skips.

The real Python exports are `MBP1Msg` and `OHLCVMsg`. The artifact's
introspection tests incorrectly used Rust-style `Mbp1Msg` and `OhlcvMsg`
names; only the tests required correction, not the production adapter.
Two additional offline regression tests construct real DBN records and
exercise their normalization through a fake transport: trades, quotes,
empty book sides using `UNDEF_PRICE`, OHLCV, symbol mappings, heartbeat,
data-loss errors, and fatal authentication errors. They confirm exact
prices against the installed `FIXED_PRICE_SCALE`, receive timestamps,
contract identity, counters, and sanitized fatal-error output. The real
`Live.__iter__` source was also inspected: it rejects iteration after
streaming has started and returns `LiveIterator` otherwise.

Startup reports ONLINE, both data services NOT CONFIGURED, and all
later phases NOT IMPLEMENTED. A Python audit hook observed zero socket
connect, DNS-resolution, or sendto attempts during startup. Ten manual
adversarial checks rejected non-boolean network flags, invalid timing
values, and real DBN trades with zero, off-tick, or undefined prices.

This is offline compatibility verification only. Actual authentication,
subscription delivery, remote symbology responses, wire streaming, and
reconnection against the provider were not tested. No live connection or
paid historical request was made. Those checks require separate explicit
authorization; Phase 5 has not begun.

### Original artifact sandbox limitations

This cloud workspace has no PyPI/package-registry network access at
all (the same constraint documented in `docs/historical_data.md` §14).
**The `databento` package could not be installed in this sandbox.**
Concretely, this means:

- `tests/test_live_data_databento.py` injects a fake live client via
  the adapter's `client_factory=` constructor parameter, and a fake
  metadata client via `metadata_client_factory=`, in every
  deterministic test, and every test for the missing-package path uses
  `sys.modules["databento"] = None` to deterministically force the
  lazy `import databento` to fail -- never
  `if "databento" in sys.modules: pytest.skip(...)`, which would behave
  differently depending on whether the real package happened to
  already be installed. This means the deterministic portion of the
  suite runs and passes unconditionally, in every environment, but the
  real `databento` package's actual live/metadata-client method
  signatures, record-field names, and wire-format details have not
  been, and could not be, exercised directly here -- **with explicit
  exceptions, all gated by `pytest.importorskip("databento")` and
  skipped outright (never silently passed) when the real package is
  absent, which is the case in this sandbox:**
  - `test_real_databento_live_and_historical_constructors_match_this_adapters_assumptions`
    inspects the real `Live.__init__`/`Historical.__init__` signatures
    via `inspect.signature` (no network, no API key) to directly
    confirm this adapter's corrected constructor-shape assumption
    against the real installed package.
  - **Phase 4.2 correction §2** added a further parametrized offline
    introspection check,
    `test_real_databento_record_classes_expose_the_attributes_this_adapter_assumes`,
    covering `TradeMsg`, `Mbp1Msg`, `OhlcvMsg`, `SymbolMappingMsg`,
    `ErrorMsg`, `SystemMsg`, and `BidAskPair` -- confirming, via
    `dir()` against the real class (located on `databento` itself, or
    `databento_dbn` as a fallback), that every attribute name this
    adapter's normalization logic assumes (e.g. `TradeMsg.sequence`,
    `OhlcvMsg.volume`, `SymbolMappingMsg.stype_out_symbol`) is still
    present on the installed version -- plus
    `test_real_databento_mbp1_exposes_a_top_of_book_representation_this_adapter_can_read`,
    confirming the real `Mbp1Msg` exposes at least one of the two
    top-of-book representations `_mbp1_top_of_book_fields` knows how to
    read. These catch an API/DBN shape mismatch directly against the
    real package, still requiring no network call and no API key.
- In particular, several specific adapter decisions are flagged above
  (§8, §11) as resting on researched, user-supplied documentation
  rather than a verified real-package run, which this correction pass
  narrows but does not fully close without the package actually
  installed: the exact `Live(key=...)`/`Historical(key=...)`
  constructor shapes and the `Live.__iter__()` auto-start lifecycle
  (Phase 4 correction §2-3, confirmed by the `inspect.signature` check
  above the moment it runs somewhere `databento` is installed); the
  exact `price`/`size` wire-field names and fixed-point scale factor
  for `TradeMsg`/`Mbp1Msg`/`OhlcvMsg` (Phase 4.2 §2's record-class
  attribute-presence checks confirm the *names* exist on the real
  classes the moment `databento` is installed, but do not confirm the
  fixed-point *scale factor* itself, which has no offline, no-network
  way to be verified); the MBP-1 top-of-book *representation* (flat
  `*_00` properties vs. a `levels` array -- Phase 4.2 §1/§2 now handles
  and checks for either, but which one the installed 0.87 version
  actually uses in practice is still unconfirmed without it installed);
  the quote side-presence convention (gated here on size rather than an
  unverified price sentinel); and the exact `symbology.resolve()` JSON
  response shape used for the live full-year identity proof (§8.1) --
  though this last one reuses, rather than reimplements, Phase 3's
  historical adapter's own already-hardened parser
  (`_distinct_resolved_instrument_id`), so it carries the same
  verification status Phase 3's one-shot historical fetches already
  carry, not a new unverified assumption. All of these should be
  re-verified against the real installed package on a machine with
  normal package-registry access before relying on this adapter for a
  real live connection.
- `requirements.txt`'s `databento` version range is carried over
  unchanged from Phase 3's own research-based pin (no new version
  research was performed specifically for the live client, since it
  ships in the same package as the historical client already pinned).

None of this affects the subscription/service/health logic that
*could* be verified without the real package (the large majority of
this subsystem), and none of it changes Phase 4's safety posture --
every gate that matters (production-domain check, network opt-in,
full-year contract identity proof, exact-contract-identity
enforcement, tick alignment, secret safety) lives in code that was
fully exercised against duck-typed fakes matching the vendor's
documented interface. It does mean the one third-party integration
point (the real Databento live wire protocol, and the metadata
resolution the identity proof depends on) carries a residual "verified
against a faithful fake, not the real package" caveat that should be
closed out by running this project's test suite with `pip install -r
requirements.txt` fully succeeded, on a machine with normal
package-registry access, before relying on this build for a real live
connection.

## 15. What a real live stream actually requires

For `RealTimeDataService.open_stream` to ever reach a real Databento
live connection, **all** of the following must be true
simultaneously: `OLIVE_LIVE_PROVIDER=databento`; a non-blank
`DATABENTO_API_KEY`; the real `databento` package installed;
`OLIVE_LIVE_NETWORK_ENABLED=true`; and a `LiveSubscriptionRequest`
naming only Olive's exact production NQ/MNQ contracts. Absent any one
of these, Olive fails closed -- `NOT_CONFIGURED`,
`LiveProviderNotConfiguredError`, or `LiveNetworkDisabledError`,
depending on which condition is missing -- and never silently
substitutes fabricated or cached data for a real live feed.
