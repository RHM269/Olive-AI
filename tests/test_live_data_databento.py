"""Phase 4 tests for app.data.providers.databento_live.DatabentoLiveProvider
and its supporting ReconnectPolicy / helper functions (Phase 4 correction
pass: rewritten against the corrected adapter -- see
app/data/providers/databento_live.py's module docstring and CLAUDE.md's
"Streaming/live-connection and counter-correctness lessons" section).

Every test injects a fake live client via ``client_factory`` AND a fake
metadata client via ``metadata_client_factory`` -- the real ``databento``
package is never required to be installed for any test in this file
except the explicit, self-skipping real-package introspection check at
the bottom (see the module's own docstring for why record classification
is duck-typed rather than isinstance-based against real databento
classes, so these fakes exercise the exact same code path whether or not
the real package happens to be installed in a given environment).

Covers: construction never opens a network connection; both the live
client's and the metadata client's own callable interfaces are
validated; connection lifecycle (connect/events/close, repeated close,
double-connect rejection); the corrected Databento 0.87 API shape is
itself enforced by the fakes (no ``dataset`` kwarg accepted by the fake
``Live`` constructor; the fake live client's ``start()`` raises if ever
called, since the corrected adapter must never call it);
auth/permission failures are never retried; transient failures ARE
retried with deterministic (non-sleeping) backoff, with the corrected,
TRUTHFUL ``reconnect_count`` (successful RE-establishments only) vs.
``reconnect_attempts_total`` (every attempt, initial or reconnect)
split; reconnect exhaustion (both at initial connect and mid-stream);
full-year contract identity proof via a fake point-in-time symbology
resolution (happy path, decade collision, wrong-decade/not-yet-listed
rejection, missing symbology capability, and a live ``SymbolMappingMsg``
that contradicts the pre-verified identity); exact-contract-identity
enforcement at the live-mapping layer (unmapped-instrument and
contradictory-remapping cases); trade/quote/bar normalization (price
fixed-point conversion, tick alignment, timestamps, the three distinct
timestamp meanings -- ``ts_event``/``ts_recv``/``olive_received_at``);
strict ``instrument_id`` type validation (bool/negative/float/string
rejected) on every record kind; unknown/control record handling; fatal
vs. non-fatal vs. data-gap ``ErrorMsg`` handling (including the
``DEGRADED`` state and its recovery); narrowly-scoped ``close()``
exception handling; secret-safety (no API key leakage); and
liveness/staleness telemetry.
"""

from __future__ import annotations

import inspect
import sys
import types
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.futures.models import ContractMonth, FuturesContract, FuturesInstrument, SettlementType
from app.data.live_models import (
    ConnectionState,
    LiveBar,
    LiveContractIdentityError,
    LiveDataError,
    LiveEventType,
    LiveIdentityUnresolvedError,
    LiveProviderAuthenticationError,
    LiveProviderNotConfiguredError,
    LiveProviderPermissionError,
    LiveProviderRateLimitError,
    LiveProviderShutdownError,
    LiveProviderUnavailableError,
    LiveQuote,
    LiveReconnectExhaustedError,
    LiveStreamClosedError,
    LiveSubscriptionRequest,
    LiveTrade,
)
import app.data.providers.databento_live as dbl

NQ = FuturesContract(root_symbol="NQ", year=2026, month=ContractMonth.DECEMBER)
MNQ = FuturesContract(root_symbol="MNQ", year=2026, month=ContractMonth.DECEMBER)
_SCALE = Decimal("1000000000")


def _price_fixed(value: str) -> int:
    return int(Decimal(value) * _SCALE)


def _instrument(root_symbol: str = "NQ", tick_size: str = "0.25") -> FuturesInstrument:
    return FuturesInstrument(
        root_symbol=root_symbol,
        display_name=f"{root_symbol} Futures",
        exchange="CME",
        underlying="Nasdaq-100",
        currency="USD",
        multiplier=Decimal("20"),
        tick_size=Decimal(tick_size),
        tick_value=Decimal("5.00"),
        settlement_type=SettlementType.CASH,
        contract_months=(ContractMonth.MARCH, ContractMonth.JUNE, ContractMonth.SEPTEMBER, ContractMonth.DECEMBER),
    )


class Rec:
    """A plain attribute bag standing in for a Databento live record --
    deliberately NOT a subclass of anything databento-specific, so
    these tests exercise the adapter's duck-typed classification path
    identically whether or not the real package is installed."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def mapping_msg(contract=NQ, instrument_id=12345, ts_event=1_700_000_000_000_000_000):
    return Rec(
        stype_in_symbol=contract.display_code,
        stype_out_symbol="instrument_id",
        instrument_id=instrument_id,
        ts_event=ts_event,
    )


def trade_msg(instrument_id=12345, price="21000.25", size=2, ts_event=1_700_000_000_000_000_000, sequence=None, ts_recv=None):
    kwargs = dict(instrument_id=instrument_id, ts_event=ts_event, price=_price_fixed(price), size=size)
    if sequence is not None:
        kwargs["sequence"] = sequence
    if ts_recv is not None:
        kwargs["ts_recv"] = ts_recv
    return Rec(**kwargs)


def quote_msg(instrument_id=12345, bid_px="21000.00", bid_sz=3, ask_px="21000.25", ask_sz=5, ts_event=1_700_000_000_000_000_000):
    level = Rec(
        bid_px=_price_fixed(bid_px) if bid_px is not None else 0,
        ask_px=_price_fixed(ask_px) if ask_px is not None else 0,
        bid_sz=bid_sz,
        ask_sz=ask_sz,
    )
    return Rec(instrument_id=instrument_id, ts_event=ts_event, levels=[level])


def bar_msg(instrument_id=12345, open_="21000", high="21001", low="20999", close="21000.5", volume=10, ts_event=1_700_000_000_000_000_000):
    return Rec(
        instrument_id=instrument_id, ts_event=ts_event,
        open=_price_fixed(open_), high=_price_fixed(high), low=_price_fixed(low), close=_price_fixed(close),
        volume=volume,
    )


def system_msg(code=0, msg="heartbeat"):
    return Rec(msg=msg, code=code)


def error_msg(err="boom", code=None, is_last=False):
    return Rec(err=err, code=code, is_last=is_last)


class _FakeCode:
    """Stands in for a databento IntEnum code member, whose own
    ``.name`` is the symbolic string the adapter reads."""

    def __init__(self, name):
        self.name = name


class FakeClient:
    """A fake Databento ``Live`` client.

    Phase 4 correction §3: ``start()`` deliberately RAISES rather than
    merely recording that it was called -- the corrected adapter must
    NEVER call it (``Live.__iter__()`` is documented to auto-start
    synchronous iteration; calling ``start()`` first and then iterating
    is documented to raise ``ValueError`` on the real client). Any
    regression back to the old, incorrect lifecycle fails every single
    test in this file immediately and loudly, rather than being
    silently tolerated by a permissive fake.
    """

    def __init__(self, records=()):
        self.records = list(records)
        self.subscribe_calls = []
        self.stopped = False

    def subscribe(self, **kwargs):
        self.subscribe_calls.append(kwargs)

    def start(self):
        raise AssertionError(
            "Phase 4 correction #3: the adapter must never call start() on the live "
            "client -- Live.__iter__() is documented to auto-start synchronous iteration."
        )

    def stop(self):
        self.stopped = True

    def __iter__(self):
        return iter(self.records)


class FakeDatabentoError(Exception):
    pass


FakeDatabentoError.__module__ = "databento.live_errors"


def make_sub(contracts=(NQ,), event_types=(LiveEventType.TRADE,)):
    return LiveSubscriptionRequest(contracts=contracts, event_types=event_types)


def make_instruments(*contracts):
    return {c.identity: _instrument(c.root_symbol) for c in contracts}


# -- Fake metadata (Historical) client: full-year identity resolution ----


def make_metadata_client(resolutions=None, *, resolve_fn=None):
    """A fake Databento ``Historical`` client exposing only the
    ``symbology.resolve`` capability this adapter actually needs.

    ``resolutions``: a ``{raw_symbol: instrument_id_str}`` mapping used
    to build a default, always-fully-resolved response for any
    requested symbol found in it (any symbol NOT in the mapping is
    reported via Databento's own documented ``not_found`` list, which
    ``_distinct_resolved_instrument_id`` treats as a hard failure).

    ``resolve_fn``: when supplied, takes full control of
    ``symbology.resolve(**kwargs)``'s return value (used by the
    collision/wrong-decade/contradiction tests below, which need to
    respond differently depending on the exact per-contract month
    window being resolved, not just the raw symbol).
    """

    class _FakeSymbology:
        def __init__(self):
            self.calls: list[dict] = []

        def resolve(self, **kwargs):
            self.calls.append(kwargs)
            if resolve_fn is not None:
                return resolve_fn(**kwargs)
            symbols = kwargs.get("symbols") or []
            result = {}
            not_found = []
            for symbol in symbols:
                if resolutions and symbol in resolutions:
                    result[symbol] = [
                        {"d0": str(kwargs.get("start_date")), "d1": str(kwargs.get("end_date")), "s": resolutions[symbol]}
                    ]
                else:
                    not_found.append(symbol)
            return {"result": result, "not_found": not_found, "partial": []}

    class _FakeHistorical:
        def __init__(self):
            self.symbology = _FakeSymbology()

    return _FakeHistorical()


# NQ/MNQ's own default instrument IDs -- deliberately matching the
# default `instrument_id=12345` used by mapping_msg()/trade_msg()/etc.
# above for NQ, so a test that doesn't care about identity resolution
# at all can just use make_provider()'s default metadata client.
DEFAULT_RESOLUTIONS = {NQ.display_code: "12345", MNQ.display_code: "67890"}


def make_provider(client_factory, *, metadata_client_factory=None, **kwargs):
    """Construct a DatabentoLiveProvider with sane defaults for both
    injectable factories -- ``client_factory`` must be a zero-arg
    callable (pass ``lambda: some_fake_client`` for a plain fake, or a
    custom tracking/raising function directly for tests that need
    that). ``metadata_client_factory`` defaults to a fake that
    authoritatively (and successfully) resolves both NQ and MNQ's
    default identities, so a test that isn't specifically about
    identity resolution never has to think about it."""
    if metadata_client_factory is None:
        metadata_client_factory = lambda: make_metadata_client(DEFAULT_RESOLUTIONS)
    kwargs.setdefault("api_key", "x")
    return dbl.DatabentoLiveProvider(client_factory=client_factory, metadata_client_factory=metadata_client_factory, **kwargs)


# -- Construction never opens a network connection ----------------------


def test_construction_with_client_factory_never_calls_it():
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        return FakeClient()

    dbl.DatabentoLiveProvider(api_key="unused", client_factory=factory)
    assert calls["n"] == 0


def test_construction_with_default_factory_never_imports_databento_eagerly():
    """Even without an injected client_factory, constructing the
    provider must not attempt to import/construct the real client --
    only connect() does."""
    provider = dbl.DatabentoLiveProvider(api_key="some-key")
    assert provider.state is ConnectionState.DISCONNECTED


@pytest.mark.parametrize("bad_key", [None, "", "   ", 123, True])
def test_rejects_malformed_api_key_when_no_factory_injected(bad_key):
    with pytest.raises(LiveProviderNotConfiguredError):
        dbl.DatabentoLiveProvider(api_key=bad_key)


@pytest.mark.parametrize("bad_key", [None, "", "   "])
def test_rejects_malformed_api_key_when_only_client_factory_injected(bad_key):
    """Injecting ONLY client_factory still leaves the default metadata
    factory in play, which still needs a real API key -- confirms the
    constructor's "either factory missing still requires a key" logic
    checks BOTH factories independently, not just client_factory."""
    with pytest.raises(LiveProviderNotConfiguredError):
        dbl.DatabentoLiveProvider(api_key=bad_key, client_factory=lambda: FakeClient())


@pytest.mark.parametrize("bad_key", [None, "", "   "])
def test_rejects_malformed_api_key_when_only_metadata_factory_injected(bad_key):
    with pytest.raises(LiveProviderNotConfiguredError):
        dbl.DatabentoLiveProvider(api_key=bad_key, metadata_client_factory=lambda: make_metadata_client(DEFAULT_RESOLUTIONS))


def test_api_key_not_required_when_both_factories_are_injected():
    # A malformed api_key is irrelevant once BOTH factories bypass the
    # default construction path entirely.
    provider = dbl.DatabentoLiveProvider(
        api_key=None,
        client_factory=lambda: FakeClient(),
        metadata_client_factory=lambda: make_metadata_client(DEFAULT_RESOLUTIONS),
    )
    assert provider.name == "databento"


@pytest.mark.parametrize("bad_dataset", [None, "", "   ", 123, True])
def test_rejects_malformed_dataset(bad_dataset):
    with pytest.raises(LiveProviderNotConfiguredError):
        dbl.DatabentoLiveProvider(api_key="key", dataset=bad_dataset, client_factory=lambda: FakeClient())


def test_default_dataset_is_glbx_mdp3():
    provider = dbl.DatabentoLiveProvider(api_key="key", client_factory=lambda: FakeClient())
    assert provider.dataset == "GLBX.MDP3"


def test_rejects_non_callable_sleep_fn():
    with pytest.raises(LiveProviderNotConfiguredError):
        dbl.DatabentoLiveProvider(api_key="key", client_factory=lambda: FakeClient(), sleep_fn="not-callable")


def test_rejects_malformed_reconnect_policy_type():
    with pytest.raises(LiveProviderNotConfiguredError):
        dbl.DatabentoLiveProvider(api_key="key", client_factory=lambda: FakeClient(), reconnect_policy="not-a-policy")


# -- Default factories: lazy import / missing package simulation --------


def test_default_metadata_factory_raises_identity_unresolved_when_package_missing():
    """Simulates a missing 'databento' package deterministically via
    sys.modules, regardless of whether it is actually installed in
    this environment -- never an environment-dependent skip. With NO
    metadata_client_factory injected, connect() reaches the default
    metadata factory FIRST (identity resolution happens before the
    live client is ever built), so this is where the missing-package
    failure surfaces."""
    sys.modules["databento"] = None
    try:
        provider = dbl.DatabentoLiveProvider(api_key="real-key")
        with pytest.raises(LiveIdentityUnresolvedError):
            provider.connect(make_sub(), make_instruments(NQ))
    finally:
        sys.modules.pop("databento", None)


def test_default_client_factory_raises_not_configured_when_package_missing():
    """Isolates the LIVE CLIENT factory's own missing-package behavior
    by injecting a working metadata_client_factory, so connect() gets
    past identity resolution and reaches _default_client_factory
    itself (which raises LiveProviderNotConfiguredError)."""
    sys.modules["databento"] = None
    try:
        provider = dbl.DatabentoLiveProvider(
            api_key="real-key", metadata_client_factory=lambda: make_metadata_client(DEFAULT_RESOLUTIONS)
        )
        with pytest.raises(LiveProviderNotConfiguredError):
            provider.connect(make_sub(), make_instruments(NQ))
    finally:
        sys.modules.pop("databento", None)


def test_default_factories_construct_real_clients_when_package_present():
    """Simulates the 'databento' package being importable (regardless
    of whether the real package is actually installed), and confirms:
    (1) the default LIVE factory calls databento.Live(key=...) with NO
    ``dataset`` kwarg (Phase 4 correction §2 -- the fake FakeLive below
    only accepts ``key``, so passing ``dataset`` would raise a
    TypeError this test would catch); (2) the default METADATA factory
    calls databento.Historical(key=...); (3) neither client's ``key``
    nor the provider's repr() ever leaks the API key."""
    fake_module = types.ModuleType("databento")
    calls = {}

    class FakeLive:
        def __init__(self, key):  # Phase 4 correction §2: key ONLY, no dataset
            calls["live_key"] = key
            self.records = []

        def subscribe(self, **kwargs):
            calls.setdefault("subscribe_calls", []).append(kwargs)

        def stop(self):
            pass

        def __iter__(self):
            return iter(self.records)

        # Deliberately no start() -- see FakeClient's docstring above;
        # this fake, standing in for the real installed package, must
        # never need one either.

    class FakeSymbology:
        def resolve(self, **kwargs):
            calls.setdefault("resolve_calls", []).append(kwargs)
            symbols = kwargs.get("symbols") or []
            return {
                "result": {sym: [{"d0": "2026-12-01", "d1": "2027-01-01", "s": "12345"}] for sym in symbols},
                "not_found": [],
                "partial": [],
            }

    class FakeHistorical:
        def __init__(self, key):
            calls["historical_key"] = key
            self.symbology = FakeSymbology()

    fake_module.Live = FakeLive
    fake_module.Historical = FakeHistorical
    sys.modules["databento"] = fake_module
    try:
        provider = dbl.DatabentoLiveProvider(api_key="real-key")
        provider.connect(make_sub(), make_instruments(NQ))
        assert calls["live_key"] == "real-key"
        assert calls["historical_key"] == "real-key"
        assert "real-key" not in repr(provider)
        assert provider.state is ConnectionState.CONNECTED
    finally:
        sys.modules.pop("databento", None)


# -- Client interface validation -----------------------------------------


@pytest.mark.parametrize("missing_method", ["subscribe", "stop"])
def test_rejects_client_missing_required_interface(missing_method):
    client = FakeClient()
    setattr(client, missing_method, None)
    provider = make_provider(lambda: client)
    with pytest.raises(LiveProviderNotConfiguredError):
        provider.connect(make_sub(), make_instruments(NQ))


def test_rejects_client_missing_iter():
    class NoIterClient:
        def subscribe(self, **kwargs):
            pass

        def stop(self):
            pass

    provider = make_provider(lambda: NoIterClient())
    with pytest.raises(LiveProviderNotConfiguredError):
        provider.connect(make_sub(), make_instruments(NQ))


def test_client_with_no_start_method_at_all_connects_successfully():
    """Phase 4 correction §3: a client that doesn't even define
    start() must connect and stream fine -- confirms the adapter's
    corrected interface requirement genuinely never needs it."""

    class MinimalClient:
        def subscribe(self, **kwargs):
            pass

        def stop(self):
            pass

        def __iter__(self):
            return iter([])

    provider = make_provider(lambda: MinimalClient())
    provider.connect(make_sub(), make_instruments(NQ))
    assert provider.state is ConnectionState.CONNECTED


def test_rejects_metadata_client_missing_symbology_capability():
    class NoSymbologyClient:
        pass

    provider = make_provider(lambda: FakeClient([]), metadata_client_factory=lambda: NoSymbologyClient())
    with pytest.raises(LiveIdentityUnresolvedError):
        provider.connect(make_sub(), make_instruments(NQ))


# -- Full-year contract identity proof (Phase 4 correction §4) -----------


def test_identity_resolution_is_scoped_to_each_contract_own_month_window():
    calls = []

    def resolve_fn(**kwargs):
        calls.append(kwargs)
        symbol = kwargs["symbols"][0]
        return {
            "result": {symbol: [{"d0": str(kwargs["start_date"]), "d1": str(kwargs["end_date"]), "s": "12345"}]},
            "not_found": [],
            "partial": [],
        }

    metadata = make_metadata_client(resolve_fn=resolve_fn)
    provider = make_provider(lambda: FakeClient([]), metadata_client_factory=lambda: metadata)
    provider.connect(make_sub(contracts=(NQ,)), make_instruments(NQ))

    assert len(calls) == 1
    call = calls[0]
    assert call["dataset"] == "GLBX.MDP3"
    assert call["symbols"] == [NQ.display_code]
    assert call["stype_in"] == "raw_symbol"
    assert call["stype_out"] == "instrument_id"
    # NQ is a December contract -- confirms the half-open month window
    # correctly rolls over into January of the FOLLOWING year.
    assert call["start_date"] == date(2026, 12, 1)
    assert call["end_date"] == date(2027, 1, 1)


def test_wrong_decade_single_contract_fails_closed_when_not_yet_listed():
    """NQ-2036-12 shares NQ-2026-12's raw symbol ("NQZ6"), but is not
    actually listed for ITS OWN month yet -- Databento's own
    documented 'not_found' classification for that exact window. Must
    fail closed (connect() rejected) rather than ever open a live
    stream, and must never guess a full year from the ambiguous raw
    symbol alone."""
    nq_2036 = FuturesContract(root_symbol="NQ", year=2036, month=ContractMonth.DECEMBER)
    assert nq_2036.display_code == NQ.display_code  # same raw symbol, different contract

    def resolve_fn(**kwargs):
        return {"result": {}, "not_found": list(kwargs["symbols"]), "partial": []}

    metadata = make_metadata_client(resolve_fn=resolve_fn)
    sub = LiveSubscriptionRequest(contracts=(nq_2036,), event_types=(LiveEventType.TRADE,))
    provider = make_provider(lambda: FakeClient([]), metadata_client_factory=lambda: metadata)
    with pytest.raises(LiveIdentityUnresolvedError):
        provider.connect(sub, make_instruments(nq_2036))
    assert provider.state is ConnectionState.FAILED


def test_nq_decade_collision_between_distinct_contracts_is_rejected():
    """Even though each contract is resolved against its OWN calendar-
    month window, two distinct Olive contracts whose resolutions
    nonetheless land on the SAME instrument_id are rejected as a
    genuine identity collision -- the defense-in-depth collision check
    itself, independent of the ordinary wrong-decade case (see
    test_wrong_decade_single_contract_fails_closed_when_not_yet_listed)
    where the far-future contract instead fails to resolve at all."""
    nq_2026 = FuturesContract(root_symbol="NQ", year=2026, month=ContractMonth.DECEMBER)
    nq_2036 = FuturesContract(root_symbol="NQ", year=2036, month=ContractMonth.DECEMBER)
    assert nq_2026.display_code == nq_2036.display_code == "NQZ6"
    assert nq_2026.identity != nq_2036.identity

    def resolve_fn(**kwargs):
        symbol = kwargs["symbols"][0]
        return {
            "result": {symbol: [{"d0": str(kwargs["start_date"]), "d1": str(kwargs["end_date"]), "s": "999999"}]},
            "not_found": [],
            "partial": [],
        }

    metadata = make_metadata_client(resolve_fn=resolve_fn)
    sub = LiveSubscriptionRequest(contracts=(nq_2026, nq_2036), event_types=(LiveEventType.TRADE,))
    provider = make_provider(lambda: FakeClient([]), metadata_client_factory=lambda: metadata)
    with pytest.raises(LiveIdentityUnresolvedError):
        provider.connect(sub, make_instruments(nq_2026, nq_2036))
    assert provider.state is ConnectionState.FAILED


def test_mnq_decade_collision_between_distinct_contracts_is_rejected():
    """The MNQ equivalent of
    test_nq_decade_collision_between_distinct_contracts_is_rejected --
    the collision check must not be NQ-specific."""
    mnq_2026 = FuturesContract(root_symbol="MNQ", year=2026, month=ContractMonth.DECEMBER)
    mnq_2036 = FuturesContract(root_symbol="MNQ", year=2036, month=ContractMonth.DECEMBER)
    assert mnq_2026.display_code == mnq_2036.display_code

    def resolve_fn(**kwargs):
        symbol = kwargs["symbols"][0]
        return {
            "result": {symbol: [{"d0": str(kwargs["start_date"]), "d1": str(kwargs["end_date"]), "s": "888888"}]},
            "not_found": [],
            "partial": [],
        }

    metadata = make_metadata_client(resolve_fn=resolve_fn)
    sub = LiveSubscriptionRequest(contracts=(mnq_2026, mnq_2036), event_types=(LiveEventType.TRADE,))
    provider = make_provider(lambda: FakeClient([]), metadata_client_factory=lambda: metadata)
    with pytest.raises(LiveIdentityUnresolvedError):
        provider.connect(sub, make_instruments(mnq_2026, mnq_2036))
    assert provider.state is ConnectionState.FAILED


def test_live_mapping_contradicting_preresolved_identity_fails_closed():
    """A live SymbolMappingMsg that disagrees with the contract's OWN
    pre-verified, authoritative identity (established in connect()
    before the stream was ever opened) must never be trusted -- this
    is what stops a single contract from being silently relabeled with
    whatever instrument_id the live stream happens to report."""
    metadata = make_metadata_client({NQ.display_code: "12345"})
    contradicting_mapping = mapping_msg(contract=NQ, instrument_id=99999)
    client = FakeClient([contradicting_mapping, trade_msg(instrument_id=99999)])
    provider = make_provider(lambda: client, metadata_client_factory=lambda: metadata)
    provider.connect(make_sub(), make_instruments(NQ))
    with pytest.raises(LiveIdentityUnresolvedError):
        list(provider.events())


def test_live_mapping_matching_preresolved_identity_is_accepted():
    metadata = make_metadata_client({NQ.display_code: "12345"})
    client = FakeClient([mapping_msg(instrument_id=12345), trade_msg(instrument_id=12345)])
    provider = make_provider(lambda: client, metadata_client_factory=lambda: metadata)
    provider.connect(make_sub(), make_instruments(NQ))
    events = list(provider.events())
    assert len(events) == 1
    assert isinstance(events[0], LiveTrade)


# -- Connection lifecycle -------------------------------------------------


def test_connect_subscribes_every_event_type_with_correct_schema_and_symbols():
    client = FakeClient([])
    sub = make_sub(contracts=(NQ, MNQ), event_types=(LiveEventType.TRADE, LiveEventType.QUOTE, LiveEventType.BAR))
    provider = make_provider(lambda: client)
    provider.connect(sub, make_instruments(NQ, MNQ))

    schemas_used = {call["schema"] for call in client.subscribe_calls}
    assert schemas_used == {"trades", "mbp-1", "ohlcv-1s"}
    for call in client.subscribe_calls:
        assert call["stype_in"] == "raw_symbol"
        assert set(call["symbols"]) == {NQ.display_code, MNQ.display_code}
        assert call["dataset"] == "GLBX.MDP3"
    # FakeClient.start() raises if ever called (see its docstring) --
    # reaching CONNECTED here already proves it never was.
    assert provider.state is ConnectionState.CONNECTED


def test_connect_rejects_subscription_missing_an_instrument():
    client = FakeClient([])
    provider = make_provider(lambda: client)
    with pytest.raises(LiveProviderNotConfiguredError):
        provider.connect(make_sub(contracts=(NQ, MNQ)), make_instruments(NQ))  # MNQ missing


def test_connect_rejects_wrong_subscription_type():
    provider = make_provider(lambda: FakeClient([]))
    with pytest.raises(LiveDataError):
        provider.connect("not a subscription", {})


def test_double_connect_without_close_is_rejected():
    provider = make_provider(lambda: FakeClient([]))
    provider.connect(make_sub(), make_instruments(NQ))
    with pytest.raises(LiveStreamClosedError):
        provider.connect(make_sub(), make_instruments(NQ))


def test_reconnect_after_close_is_allowed():
    provider = make_provider(lambda: FakeClient([]))
    provider.connect(make_sub(), make_instruments(NQ))
    provider.close()
    provider.connect(make_sub(), make_instruments(NQ))  # must not raise
    assert provider.state is ConnectionState.CONNECTED


def test_close_before_connect_is_safe():
    provider = make_provider(lambda: FakeClient([]))
    provider.close()
    assert provider.state is ConnectionState.STOPPED


def test_close_is_idempotent():
    provider = make_provider(lambda: FakeClient([]))
    provider.connect(make_sub(), make_instruments(NQ))
    provider.close()
    provider.close()
    provider.close()
    assert provider.state is ConnectionState.STOPPED


def test_events_before_connect_raises():
    provider = make_provider(lambda: FakeClient([]))
    with pytest.raises(LiveStreamClosedError):
        next(provider.events())


def test_events_after_close_raises():
    provider = make_provider(lambda: FakeClient([]))
    provider.connect(make_sub(), make_instruments(NQ))
    provider.close()
    with pytest.raises(LiveStreamClosedError):
        next(provider.events())


def test_clean_stream_end_transitions_to_stopped():
    client = FakeClient([mapping_msg(), trade_msg()])
    provider = make_provider(lambda: client)
    provider.connect(make_sub(), make_instruments(NQ))
    events = list(provider.events())
    assert len(events) == 1
    assert provider.state is ConnectionState.STOPPED


# -- close(): narrow exception handling (Phase 4 correction §7) ----------


def test_close_suppresses_a_recognized_databento_shutdown_exception():
    class FlakyStopClient(FakeClient):
        def stop(self):
            raise FakeDatabentoError("already disconnected")

    client = FlakyStopClient()
    provider = make_provider(lambda: client)
    provider.connect(make_sub(), make_instruments(NQ))
    provider.close()  # must not raise
    assert provider.state is ConnectionState.STOPPED


def test_close_reraises_an_unrecognized_exception_from_stop():
    class BuggyStopClient(FakeClient):
        def stop(self):
            raise RuntimeError("programming bug, not a databento-origin condition")

    client = BuggyStopClient()
    provider = make_provider(lambda: client)
    provider.connect(make_sub(), make_instruments(NQ))
    with pytest.raises(RuntimeError):
        provider.close()
    # finally: still reaches a coherent STOPPED state even though the
    # unexpected exception propagated.
    assert provider.state is ConnectionState.STOPPED


def test_close_translates_an_unexpected_databento_vendor_exception_into_shutdown_error():
    """Phase 4.2 correction §3: a databento-origin exception that does
    NOT match any recognized benign shutdown condition is no longer
    silently suppressed just because its module starts with
    'databento' -- it must be translated into Olive's own
    LiveProviderShutdownError and raised, never discarded."""

    class UnexpectedVendorFailureClient(FakeClient):
        def stop(self):
            raise FakeDatabentoError("internal session corruption detected")

    client = UnexpectedVendorFailureClient()
    provider = make_provider(lambda: client)
    provider.connect(make_sub(), make_instruments(NQ))
    with pytest.raises(LiveProviderShutdownError):
        provider.close()
    assert provider.state is ConnectionState.STOPPED


def test_close_shutdown_error_never_leaks_the_underlying_vendor_exception_text():
    class UnexpectedVendorFailureClient(FakeClient):
        def stop(self):
            raise FakeDatabentoError("session token sk-super-secret-xyz987 is invalid")

    client = UnexpectedVendorFailureClient()
    provider = make_provider(lambda: client)
    provider.connect(make_sub(), make_instruments(NQ))
    with pytest.raises(LiveProviderShutdownError) as excinfo:
        provider.close()
    assert "sk-super-secret-xyz987" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None  # severed via `from None`


@pytest.mark.parametrize(
    "benign_message",
    ["already stopped", "Connection already closed", "ALREADY DISCONNECTED", "not connected", "no active session"],
)
def test_close_suppresses_every_recognized_benign_shutdown_condition(benign_message):
    class FlakyStopClient(FakeClient):
        def stop(self):
            raise FakeDatabentoError(benign_message)

    client = FlakyStopClient()
    provider = make_provider(lambda: client)
    provider.connect(make_sub(), make_instruments(NQ))
    provider.close()  # must not raise
    assert provider.state is ConnectionState.STOPPED


def test_close_state_is_stopped_after_every_shutdown_path():
    """The required state-after-every-path matrix (Phase 4.2 §3): a
    coherent STOPPED state is reached whether stop() succeeds, raises a
    recognized-benign condition, raises an unexpected vendor error, or
    raises a non-vendor bug."""

    def make(stop_fn):
        class C(FakeClient):
            def stop(self):
                stop_fn()

        return make_provider(lambda: C())

    # Normal stop.
    provider = make(lambda: None)
    provider.connect(make_sub(), make_instruments(NQ))
    provider.close()
    assert provider.state is ConnectionState.STOPPED

    # Known benign shutdown condition.
    provider = make(lambda: (_ for _ in ()).throw(FakeDatabentoError("already stopped")))
    provider.connect(make_sub(), make_instruments(NQ))
    provider.close()
    assert provider.state is ConnectionState.STOPPED

    # Unexpected databento vendor exception.
    provider = make(lambda: (_ for _ in ()).throw(FakeDatabentoError("internal session corruption")))
    provider.connect(make_sub(), make_instruments(NQ))
    with pytest.raises(LiveProviderShutdownError):
        provider.close()
    assert provider.state is ConnectionState.STOPPED

    # Unexpected non-vendor exception.
    provider = make(lambda: (_ for _ in ()).throw(RuntimeError("programming bug")))
    provider.connect(make_sub(), make_instruments(NQ))
    with pytest.raises(RuntimeError):
        provider.close()
    assert provider.state is ConnectionState.STOPPED


# -- Secret safety ---------------------------------------------------------


def test_auth_failure_never_leaks_api_key_in_exception_text():
    def factory():
        raise FakeDatabentoError("Invalid username in Basic auth ('sk-super-secret-abc123')")

    provider = make_provider(factory)
    with pytest.raises(LiveProviderAuthenticationError) as excinfo:
        provider.connect(make_sub(), make_instruments(NQ))
    assert "sk-super-secret-abc123" not in str(excinfo.value)


def test_auth_failure_never_leaks_api_key_via_traceback_chain():
    def factory():
        raise FakeDatabentoError("Unauthorized: invalid API key sk-super-secret-abc123")

    provider = make_provider(factory)
    with pytest.raises(LiveProviderAuthenticationError) as excinfo:
        provider.connect(make_sub(), make_instruments(NQ))
    assert excinfo.value.__cause__ is None  # severed via `from None`


# -- Auth/permission failures: never retried ------------------------------


def test_auth_failure_is_never_retried():
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        raise FakeDatabentoError("unauthorized")

    provider = make_provider(
        factory,
        reconnect_policy=dbl.ReconnectPolicy(max_attempts=5, base_delay_seconds=Decimal("1"), max_delay_seconds=Decimal("5")),
        sleep_fn=lambda s: pytest.fail("must never sleep for a permanent auth failure"),
    )
    with pytest.raises(LiveProviderAuthenticationError):
        provider.connect(make_sub(), make_instruments(NQ))
    assert calls["n"] == 1
    assert provider.state is ConnectionState.FAILED


def test_permission_failure_is_never_retried():
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        raise FakeDatabentoError("forbidden: not entitled to this dataset")

    provider = make_provider(
        factory,
        sleep_fn=lambda s: pytest.fail("must never sleep for a permanent permission failure"),
    )
    with pytest.raises(LiveProviderPermissionError):
        provider.connect(make_sub(), make_instruments(NQ))
    assert calls["n"] == 1
    assert provider.state is ConnectionState.FAILED


# -- Transient failures: deterministic retry, truthful reconnect telemetry -


def test_initial_connection_succeeds_immediately_counts_nothing():
    client = FakeClient([])
    provider = make_provider(lambda: client)
    provider.connect(make_sub(), make_instruments(NQ))
    status = provider.status
    assert status.reconnect_count == 0
    assert status.reconnect_attempts_total == 0


def test_transient_failure_then_success_reconnects_with_backoff_and_no_sleep():
    calls = {"n": 0}
    good_client = FakeClient([])

    def factory():
        calls["n"] += 1
        if calls["n"] < 3:
            raise FakeDatabentoError("connection reset")
        return good_client

    sleeps = []
    provider = make_provider(
        factory,
        reconnect_policy=dbl.ReconnectPolicy(max_attempts=5, base_delay_seconds=Decimal("1"), max_delay_seconds=Decimal("10")),
        sleep_fn=lambda s: sleeps.append(s),
    )
    provider.connect(make_sub(), make_instruments(NQ))
    assert calls["n"] == 3
    assert sleeps == [1.0, 2.0]  # exact bounded-exponential sequence, never real time.sleep
    assert provider.state is ConnectionState.CONNECTED
    status = provider.status
    # Phase 4 correction §6: this is the INITIAL connection -- however
    # many attempts it took, no PRIOR successful connection was ever
    # lost, so reconnect_count (successful RE-establishments only)
    # must stay 0. The 2 failed attempts belong in
    # reconnect_attempts_total instead.
    assert status.reconnect_count == 0
    assert status.reconnect_attempts_total == 2


def test_reconnect_exhaustion_raises_and_marks_failed():
    def factory():
        raise FakeDatabentoError("still down")

    provider = make_provider(
        factory,
        reconnect_policy=dbl.ReconnectPolicy(max_attempts=2, base_delay_seconds=Decimal("1"), max_delay_seconds=Decimal("2")),
        sleep_fn=lambda s: None,
    )
    with pytest.raises(LiveReconnectExhaustedError):
        provider.connect(make_sub(), make_instruments(NQ))
    assert provider.state is ConnectionState.FAILED


def test_transient_failure_mid_stream_reconnects_and_resumes():
    """A transient error raised while iterating an already-open
    client must trigger a reconnect (fresh client_factory() call) and
    resume consumption, rather than killing the whole stream."""

    class DroppingClient:
        def __init__(self, calls):
            self.calls = calls

        def subscribe(self, **kwargs):
            pass

        def stop(self):
            pass

        def __iter__(self):
            self.calls["n"] += 1
            if self.calls["n"] == 1:
                def gen():
                    yield mapping_msg()
                    raise FakeDatabentoError("mid-stream connection drop")

                return gen()
            return iter([trade_msg()])

    calls = {"n": 0}
    dropping_client = DroppingClient(calls)
    provider = make_provider(lambda: dropping_client, sleep_fn=lambda s: None)
    provider.connect(make_sub(), make_instruments(NQ))
    events = list(provider.events())
    assert len(events) == 1
    assert isinstance(events[0], LiveTrade)
    status = provider.status
    # Exactly one PRIOR, already-successful connection was lost and
    # then re-established -- and it succeeded on the very first
    # attempt, so reconnect_attempts_total stays 0.
    assert status.reconnect_count == 1
    assert status.reconnect_attempts_total == 0


def test_transient_failure_mid_stream_reconnects_after_two_failed_attempts():
    """The fuller reconnect-telemetry matrix (Phase 4 correction §6):
    an established stream drops, two reconnect ATTEMPTS fail, and the
    third succeeds -- reconnect_count must still read exactly 1 (one
    successful recovery), with reconnect_attempts_total reading 2 (the
    two failures that preceded it)."""

    class DroppingClient:
        def subscribe(self, **kwargs):
            pass

        def stop(self):
            pass

        def __iter__(self):
            def gen():
                yield mapping_msg()
                raise FakeDatabentoError("mid-stream connection drop")

            return gen()

    good_client = FakeClient([trade_msg()])
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        if calls["n"] == 1:
            return DroppingClient()
        if calls["n"] in (2, 3):
            raise FakeDatabentoError("still down")
        return good_client

    provider = make_provider(
        factory,
        reconnect_policy=dbl.ReconnectPolicy(max_attempts=5, base_delay_seconds=Decimal("1"), max_delay_seconds=Decimal("5")),
        sleep_fn=lambda s: None,
    )
    provider.connect(make_sub(), make_instruments(NQ))
    events = list(provider.events())
    assert len(events) == 1
    assert isinstance(events[0], LiveTrade)
    status = provider.status
    assert status.reconnect_count == 1
    assert status.reconnect_attempts_total == 2


def test_transient_failure_mid_stream_exhaustion_marks_failed():
    """A mid-stream drop whose reconnect attempts are then ALL
    exhausted must raise LiveReconnectExhaustedError and leave the
    provider FAILED -- the mid-stream counterpart to
    test_reconnect_exhaustion_raises_and_marks_failed (which only
    covers exhaustion during the INITIAL connection)."""

    def factory_sequence():
        calls = {"n": 0}

        def factory():
            calls["n"] += 1
            if calls["n"] == 1:
                class DroppingClient:
                    def subscribe(self, **kwargs):
                        pass

                    def stop(self):
                        pass

                    def __iter__(self):
                        def gen():
                            yield mapping_msg()
                            raise FakeDatabentoError("mid-stream connection drop")

                        return gen()

                return DroppingClient()
            raise FakeDatabentoError("still down")

        return factory

    provider = make_provider(
        factory_sequence(),
        reconnect_policy=dbl.ReconnectPolicy(max_attempts=2, base_delay_seconds=Decimal("1"), max_delay_seconds=Decimal("2")),
        sleep_fn=lambda s: None,
    )
    provider.connect(make_sub(), make_instruments(NQ))
    with pytest.raises(LiveReconnectExhaustedError):
        list(provider.events())
    assert provider.state is ConnectionState.FAILED


# -- Old-client cleanup during mid-stream reconnect (Phase 4.2 §4) -------


def test_old_client_is_stopped_before_being_replaced_on_mid_stream_reconnect():
    """Before replacing self._client in the mid-stream recovery path,
    the adapter must explicitly attempt to stop the OLD client -- an
    iterator exception does not guarantee every underlying
    resource/connection on that old client has already been released."""

    class DroppingClient:
        def __init__(self):
            self.stopped = False

        def subscribe(self, **kwargs):
            pass

        def stop(self):
            self.stopped = True

        def __iter__(self):
            def gen():
                yield mapping_msg()
                raise FakeDatabentoError("mid-stream connection drop")

            return gen()

    dropping_client = DroppingClient()
    good_client = FakeClient([trade_msg()])
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        if calls["n"] == 1:
            return dropping_client
        return good_client

    provider = make_provider(factory, sleep_fn=lambda s: None)
    provider.connect(make_sub(), make_instruments(NQ))
    events = list(provider.events())
    assert len(events) == 1
    assert dropping_client.stopped is True


def test_old_client_cleanup_absorbs_a_databento_exception_from_the_old_clients_stop():
    """Cleanup of the old client uses the MORE permissive semantics of
    §4 (not the narrower §3 close() policy): ANY databento-origin
    exception raised while stopping the old, already-broken,
    about-to-be-discarded client -- benign or not -- must never prevent
    the reconnect from proceeding."""

    class DroppingClient:
        def subscribe(self, **kwargs):
            pass

        def stop(self):
            raise FakeDatabentoError("internal session corruption")  # NOT a recognized benign condition

        def __iter__(self):
            def gen():
                yield mapping_msg()
                raise FakeDatabentoError("mid-stream connection drop")

            return gen()

    good_client = FakeClient([trade_msg()])
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        if calls["n"] == 1:
            return DroppingClient()
        return good_client

    provider = make_provider(factory, sleep_fn=lambda s: None)
    provider.connect(make_sub(), make_instruments(NQ))
    events = list(provider.events())  # must not raise despite the old client's stop() failing
    assert len(events) == 1
    assert isinstance(events[0], LiveTrade)
    assert provider.status.reconnect_count == 1


def test_old_client_cleanup_non_vendor_exception_still_propagates():
    """A non-databento-origin exception from the old client's stop()
    is a genuine programming bug, not a vendor shutdown quirk -- §4's
    more permissive semantics only extend to databento-origin
    exceptions, exactly as in §3."""

    class DroppingClient:
        def subscribe(self, **kwargs):
            pass

        def stop(self):
            raise RuntimeError("programming bug, not a databento-origin condition")

        def __iter__(self):
            def gen():
                yield mapping_msg()
                raise FakeDatabentoError("mid-stream connection drop")

            return gen()

    provider = make_provider(lambda: DroppingClient(), sleep_fn=lambda s: None)
    provider.connect(make_sub(), make_instruments(NQ))
    with pytest.raises(RuntimeError):
        list(provider.events())


def test_successful_mid_stream_reconnect_does_not_cause_duplicate_subscriptions():
    """Cleanup of the old client must not cause the new client to be
    subscribed more than once."""

    class DroppingClient:
        def subscribe(self, **kwargs):
            pass

        def stop(self):
            pass

        def __iter__(self):
            def gen():
                yield mapping_msg()
                raise FakeDatabentoError("mid-stream connection drop")

            return gen()

    good_client = FakeClient([trade_msg()])
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        if calls["n"] == 1:
            return DroppingClient()
        return good_client

    provider = make_provider(factory, sleep_fn=lambda s: None)
    provider.connect(make_sub(), make_instruments(NQ))
    list(provider.events())
    assert len(good_client.subscribe_calls) == 1


def test_no_old_client_remains_referenced_as_the_active_client_after_successful_replacement():
    class DroppingClient:
        def subscribe(self, **kwargs):
            pass

        def stop(self):
            pass

        def __iter__(self):
            def gen():
                yield mapping_msg()
                raise FakeDatabentoError("mid-stream connection drop")

            return gen()

    dropping_client = DroppingClient()
    good_client = FakeClient([trade_msg()])
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        if calls["n"] == 1:
            return dropping_client
        return good_client

    provider = make_provider(factory, sleep_fn=lambda s: None)
    provider.connect(make_sub(), make_instruments(NQ))
    list(provider.events())
    assert provider._client is good_client
    assert provider._client is not dropping_client


# -- Exact contract identity enforcement at the live-mapping layer -------


def test_trade_before_mapping_message_is_rejected_not_raised():
    client = FakeClient([trade_msg(instrument_id=999)])  # no mapping msg received
    provider = make_provider(lambda: client)
    provider.connect(make_sub(), make_instruments(NQ))
    events = list(provider.events())
    assert events == []
    assert provider.status.events_rejected == 1
    assert provider.status.events_received == 1


def test_mapping_for_unsubscribed_symbol_is_rejected():
    other_contract = FuturesContract(root_symbol="MNQ", year=2026, month=ContractMonth.DECEMBER)
    unrelated_mapping = Rec(
        stype_in_symbol=other_contract.display_code, stype_out_symbol="instrument_id",
        instrument_id=555, ts_event=1_700_000_000_000_000_000,
    )
    client = FakeClient([unrelated_mapping, trade_msg(instrument_id=555)])
    provider = make_provider(lambda: client)
    provider.connect(make_sub(contracts=(NQ,)), make_instruments(NQ))  # only NQ subscribed
    events = list(provider.events())
    assert events == []
    assert provider.status.events_rejected == 2  # the mapping AND the trade


def test_contradictory_remapping_of_same_instrument_id_fails_closed():
    """This is a defense-in-depth check inside ``_record_symbol_mapping``
    itself. It is provably UNREACHABLE via the public connect()/events()
    API now that connect()'s own pre-verification (see
    test_nq_decade_collision_between_distinct_contracts_is_rejected)
    already enforces globally-distinct expected instrument IDs per
    contract -- two different contracts can never legitimately match
    the same live-reported instrument_id once that invariant holds.
    It still guards against a BUG in that pre-verification itself, so
    it is exercised directly here by manually constructing the
    otherwise-impossible internal state it defends against, rather
    than through connect()/events()."""
    sub = make_sub(contracts=(NQ, MNQ), event_types=(LiveEventType.TRADE,))
    metadata = make_metadata_client({NQ.display_code: "777", MNQ.display_code: "888"})
    provider = make_provider(lambda: FakeClient([]), metadata_client_factory=lambda: metadata)
    provider.connect(sub, make_instruments(NQ, MNQ))

    # A valid mapping first legitimately associates instrument_id
    # "777" with NQ (NQ's own pre-verified expected id).
    provider._record_symbol_mapping(mapping_msg(contract=NQ, instrument_id=777))
    assert provider._instrument_id_to_contract["777"].identity == NQ.identity

    # Simulate a bug in pre-verification that let MNQ's own expected
    # id collide with NQ's (connect()'s own collision check would
    # normally make this state impossible to reach -- see the module
    # docstring). With that corrupted state, MNQ's own mapping message
    # would now also match instrument_id "777", contradicting the
    # already-established NQ mapping.
    provider._expected_instrument_ids[MNQ.identity] = "777"
    with pytest.raises(LiveContractIdentityError):
        provider._record_symbol_mapping(mapping_msg(contract=MNQ, instrument_id=777))


def test_remapping_same_instrument_to_same_contract_is_not_contradictory():
    client = FakeClient([mapping_msg(instrument_id=111), mapping_msg(instrument_id=111), trade_msg(instrument_id=111)])
    metadata = make_metadata_client({NQ.display_code: "111"})
    provider = make_provider(lambda: client, metadata_client_factory=lambda: metadata)
    provider.connect(make_sub(), make_instruments(NQ))
    events = list(provider.events())
    assert len(events) == 1


# -- instrument_id strict type validation (Phase 4 correction §10) -------


@pytest.mark.parametrize("bad_instrument_id", [True, False, -5, 0, 12.5, "12345", None])
def test_malformed_instrument_id_on_trade_record_is_rejected(bad_instrument_id):
    client = FakeClient([mapping_msg(), trade_msg(instrument_id=bad_instrument_id)])
    provider = make_provider(lambda: client)
    provider.connect(make_sub(), make_instruments(NQ))
    events = list(provider.events())
    assert events == []
    assert provider.status.events_rejected == 1


@pytest.mark.parametrize("bad_instrument_id", [True, False, -5, 0, 12.5, "12345", None])
def test_malformed_instrument_id_on_symbol_mapping_is_rejected(bad_instrument_id):
    client = FakeClient([mapping_msg(instrument_id=bad_instrument_id), trade_msg()])
    provider = make_provider(lambda: client)
    provider.connect(make_sub(), make_instruments(NQ))
    events = list(provider.events())
    assert events == []
    # the malformed mapping itself, AND the now-orphaned trade (never
    # matched to any contract), are both rejected.
    assert provider.status.events_rejected == 2


# -- Trade normalization ---------------------------------------------------


def test_trade_normalization_happy_path():
    client = FakeClient([mapping_msg(), trade_msg(price="21000.25", size=4, sequence=7)])
    provider = make_provider(lambda: client)
    provider.connect(make_sub(), make_instruments(NQ))
    events = list(provider.events())
    assert len(events) == 1
    trade = events[0]
    assert isinstance(trade, LiveTrade)
    assert trade.price == Decimal("21000.25")
    assert trade.size == 4
    assert trade.sequence == 7
    assert trade.provider == "databento"
    assert trade.dataset == "GLBX.MDP3"
    assert trade.provider_raw_symbol == NQ.display_code
    assert trade.provider_instrument_id == "12345"
    assert trade.ts_event.tzinfo is not None


def test_trade_with_ts_recv_preserves_both_timestamps_distinctly():
    client = FakeClient([
        mapping_msg(),
        trade_msg(ts_event=1_700_000_000_000_000_000, ts_recv=1_700_000_000_002_000_000),
    ])
    provider = make_provider(lambda: client)
    provider.connect(make_sub(), make_instruments(NQ))
    trade = list(provider.events())[0]
    assert trade.ts_event != trade.ts_recv
    assert trade.ts_recv > trade.ts_event


def test_trade_with_tick_misaligned_price_is_rejected_not_raised():
    client = FakeClient([mapping_msg(), trade_msg(price="21000.10")])  # not a multiple of 0.25
    provider = make_provider(lambda: client)
    provider.connect(make_sub(), make_instruments(NQ))
    events = list(provider.events())
    assert events == []
    assert provider.status.events_rejected == 1


def test_trade_with_zero_price_is_rejected():
    client = FakeClient([mapping_msg(), trade_msg(price="0")])
    provider = make_provider(lambda: client)
    provider.connect(make_sub(), make_instruments(NQ))
    assert list(provider.events()) == []
    assert provider.status.events_rejected == 1


def test_trade_with_bool_size_is_rejected():
    client = FakeClient([mapping_msg(), Rec(instrument_id=12345, ts_event=1_700_000_000_000_000_000, price=_price_fixed("100"), size=True)])
    provider = make_provider(lambda: client)
    provider.connect(make_sub(), make_instruments(NQ))
    assert list(provider.events()) == []
    assert provider.status.events_rejected == 1


# -- olive_received_at: the three distinct timestamp meanings ------------


def test_trade_preserves_three_distinct_timestamp_meanings():
    client = FakeClient([
        mapping_msg(),
        trade_msg(ts_event=1_700_000_000_000_000_000, ts_recv=1_700_000_000_002_000_000),
    ])
    provider = make_provider(lambda: client)
    provider.connect(make_sub(), make_instruments(NQ))
    trade = list(provider.events())[0]
    assert trade.olive_received_at is not None
    assert trade.olive_received_at.tzinfo is not None
    # ts_event (exchange/event time), ts_recv (Databento's own receive
    # time), and olive_received_at (Olive's local wall-clock receive
    # time) are three independently-meaningful timestamps -- none may
    # be silently conflated with another.
    assert trade.olive_received_at != trade.ts_event
    assert trade.olive_received_at != trade.ts_recv
    assert trade.ts_event != trade.ts_recv


def test_quote_and_bar_also_populate_olive_received_at():
    client = FakeClient([mapping_msg(), quote_msg()])
    provider = make_provider(lambda: client)
    provider.connect(make_sub(event_types=(LiveEventType.QUOTE,)), make_instruments(NQ))
    quote = list(provider.events())[0]
    assert quote.olive_received_at is not None
    assert quote.olive_received_at.tzinfo is not None

    client2 = FakeClient([mapping_msg(), bar_msg()])
    provider2 = make_provider(lambda: client2)
    provider2.connect(make_sub(event_types=(LiveEventType.BAR,)), make_instruments(NQ))
    bar = list(provider2.events())[0]
    assert bar.olive_received_at is not None
    assert bar.olive_received_at.tzinfo is not None


# -- Quote normalization ----------------------------------------------------


def test_quote_normalization_both_sides():
    client = FakeClient([mapping_msg(), quote_msg(bid_px="21000.00", bid_sz=3, ask_px="21000.25", ask_sz=5)])
    provider = make_provider(lambda: client)
    provider.connect(make_sub(event_types=(LiveEventType.QUOTE,)), make_instruments(NQ))
    quote = list(provider.events())[0]
    assert isinstance(quote, LiveQuote)
    assert quote.bid_price == Decimal("21000.00")
    assert quote.bid_size == 3
    assert quote.ask_price == Decimal("21000.25")
    assert quote.ask_size == 5


def test_quote_normalization_one_sided_ask_absent_via_zero_size():
    client = FakeClient([mapping_msg(), quote_msg(bid_px="21000.00", bid_sz=3, ask_px="0", ask_sz=0)])
    provider = make_provider(lambda: client)
    provider.connect(make_sub(event_types=(LiveEventType.QUOTE,)), make_instruments(NQ))
    quote = list(provider.events())[0]
    assert quote.bid_price == Decimal("21000.00")
    assert quote.ask_price is None
    assert quote.ask_size is None


def test_quote_with_tick_misaligned_bid_is_rejected():
    client = FakeClient([mapping_msg(), quote_msg(bid_px="21000.10", bid_sz=3, ask_px="0", ask_sz=0)])
    provider = make_provider(lambda: client)
    provider.connect(make_sub(event_types=(LiveEventType.QUOTE,)), make_instruments(NQ))
    assert list(provider.events()) == []
    assert provider.status.events_rejected == 1


# -- MBP-1 real Python record compatibility (Phase 4.2 correction §1) ----


class _IndexableLevels:
    """Stands in for Databento's real top-of-book array type backing
    ``MBP1Msg.levels`` -- indexable via ``__getitem__`` exactly like a
    list, but deliberately NOT a subclass of ``list`` or ``tuple``, so
    a correct implementation must never gate quote extraction on
    ``isinstance(levels, (list, tuple))`` (Phase 4.2 correction §1: the
    prior isinstance-based check silently treated this container as
    absent and fell back to the whole MBP-1 message itself, which
    exposes no bid_px/ask_px/bid_sz/ask_sz of its own, rather than
    raising)."""

    def __init__(self, items):
        self._items = list(items)

    def __getitem__(self, index):
        return self._items[index]

    def __len__(self):
        return len(self._items)


def quote_msg_with_indexable_levels(
    instrument_id=12345, bid_px="21000.00", bid_sz=3, ask_px="21000.25", ask_sz=5, ts_event=1_700_000_000_000_000_000
):
    level = Rec(bid_px=_price_fixed(bid_px), ask_px=_price_fixed(ask_px), bid_sz=bid_sz, ask_sz=ask_sz)
    return Rec(instrument_id=instrument_id, ts_event=ts_event, levels=_IndexableLevels([level]))


def quote_msg_with_flat_00_properties(
    instrument_id=12345, bid_px="21000.00", bid_sz=3, ask_px="21000.25", ask_sz=5, ts_event=1_700_000_000_000_000_000
):
    """Databento 0.87's real MBP1Msg also exposes top-of-book via flat
    per-level properties -- no 'levels' array required at all."""
    return Rec(
        instrument_id=instrument_id,
        ts_event=ts_event,
        bid_px_00=_price_fixed(bid_px),
        ask_px_00=_price_fixed(ask_px),
        bid_sz_00=bid_sz,
        ask_sz_00=ask_sz,
    )


def test_quote_normalization_uses_non_list_tuple_indexable_levels_container():
    """A plain Python list (as quote_msg() above deliberately always
    uses, for the many tests that don't care about this distinction)
    would silently hide an isinstance(levels, (list, tuple)) regression
    -- this test uses a container that is indexable but genuinely NOT a
    list or tuple, exactly like the real Databento BidAskPair array."""
    client = FakeClient([mapping_msg(), quote_msg_with_indexable_levels()])
    provider = make_provider(lambda: client)
    provider.connect(make_sub(event_types=(LiveEventType.QUOTE,)), make_instruments(NQ))
    quote = list(provider.events())[0]
    assert isinstance(quote, LiveQuote)
    assert quote.bid_price == Decimal("21000.00")
    assert quote.bid_size == 3
    assert quote.ask_price == Decimal("21000.25")
    assert quote.ask_size == 5


def test_quote_normalization_prefers_real_flat_00_properties():
    client = FakeClient([mapping_msg(), quote_msg_with_flat_00_properties()])
    provider = make_provider(lambda: client)
    provider.connect(make_sub(event_types=(LiveEventType.QUOTE,)), make_instruments(NQ))
    quote = list(provider.events())[0]
    assert isinstance(quote, LiveQuote)
    assert quote.bid_price == Decimal("21000.00")
    assert quote.bid_size == 3
    assert quote.ask_price == Decimal("21000.25")
    assert quote.ask_size == 5


def test_quote_normalization_flat_00_properties_take_priority_over_levels():
    """When a record exposes both representations, the flat *_00
    properties (the real 0.87 primary API) win; 'levels' is only a
    fallback for records that expose nothing else."""
    level = Rec(bid_px=_price_fixed("1.00"), ask_px=_price_fixed("1.25"), bid_sz=1, ask_sz=1)
    record = Rec(
        instrument_id=12345,
        ts_event=1_700_000_000_000_000_000,
        levels=_IndexableLevels([level]),
        bid_px_00=_price_fixed("21000.00"),
        ask_px_00=_price_fixed("21000.25"),
        bid_sz_00=3,
        ask_sz_00=5,
    )
    client = FakeClient([mapping_msg(), record])
    provider = make_provider(lambda: client)
    provider.connect(make_sub(event_types=(LiveEventType.QUOTE,)), make_instruments(NQ))
    quote = list(provider.events())[0]
    assert quote.bid_price == Decimal("21000.00")
    assert quote.ask_price == Decimal("21000.25")


def test_quote_with_missing_levels_and_no_flat_properties_is_rejected():
    """A quote record exposing neither the flat *_00 properties nor a
    usable 'levels' entry must be rejected with an Olive-owned data
    error -- never silently treated as an empty/no-op quote by falling
    back to the raw message object itself."""
    record = Rec(instrument_id=12345, ts_event=1_700_000_000_000_000_000)
    client = FakeClient([mapping_msg(), record])
    provider = make_provider(lambda: client)
    provider.connect(make_sub(event_types=(LiveEventType.QUOTE,)), make_instruments(NQ))
    assert list(provider.events()) == []
    assert provider.status.events_rejected == 1


def test_quote_with_empty_levels_container_is_rejected():
    record = Rec(instrument_id=12345, ts_event=1_700_000_000_000_000_000, levels=_IndexableLevels([]))
    client = FakeClient([mapping_msg(), record])
    provider = make_provider(lambda: client)
    provider.connect(make_sub(event_types=(LiveEventType.QUOTE,)), make_instruments(NQ))
    assert list(provider.events()) == []
    assert provider.status.events_rejected == 1


def test_quote_with_levels_top_entry_missing_all_expected_attributes_is_rejected():
    """A 'levels[0]' entry that itself exposes none of
    bid_px/ask_px/bid_sz/ask_sz (a malformed/unrecognized top-of-book
    object) must also be rejected, rather than silently treated as an
    all-None quote."""
    useless_top = Rec(some_other_field=1)
    record = Rec(instrument_id=12345, ts_event=1_700_000_000_000_000_000, levels=_IndexableLevels([useless_top]))
    client = FakeClient([mapping_msg(), record])
    provider = make_provider(lambda: client)
    provider.connect(make_sub(event_types=(LiveEventType.QUOTE,)), make_instruments(NQ))
    assert list(provider.events()) == []
    assert provider.status.events_rejected == 1


# -- Bar normalization --------------------------------------------------


def test_bar_normalization_happy_path():
    client = FakeClient([mapping_msg(), bar_msg(open_="21000", high="21001", low="20999", close="21000.5", volume=42)])
    provider = make_provider(lambda: client)
    provider.connect(make_sub(event_types=(LiveEventType.BAR,)), make_instruments(NQ))
    bar = list(provider.events())[0]
    assert isinstance(bar, LiveBar)
    assert bar.open == Decimal("21000")
    assert bar.high == Decimal("21001")
    assert bar.low == Decimal("20999")
    assert bar.close == Decimal("21000.5")
    assert bar.volume == 42
    from app.data.models import HistoricalTimeframe

    assert bar.interval is HistoricalTimeframe.ONE_SECOND


def test_bar_with_tick_misaligned_close_is_rejected():
    client = FakeClient([mapping_msg(), bar_msg(close="21000.52")])  # not a 0.25 multiple
    provider = make_provider(lambda: client)
    provider.connect(make_sub(event_types=(LiveEventType.BAR,)), make_instruments(NQ))
    assert list(provider.events()) == []
    assert provider.status.events_rejected == 1


def test_bar_with_invalid_ohlc_relationship_is_rejected():
    client = FakeClient([mapping_msg(), bar_msg(high="20900")])  # high < low -- invalid
    provider = make_provider(lambda: client)
    provider.connect(make_sub(event_types=(LiveEventType.BAR,)), make_instruments(NQ))
    assert list(provider.events()) == []
    assert provider.status.events_rejected == 1


# -- Control/system/unknown records ---------------------------------------


def test_system_message_is_counted_but_never_yielded_or_rejected():
    client = FakeClient([mapping_msg(), system_msg(), trade_msg()])
    provider = make_provider(lambda: client)
    provider.connect(make_sub(), make_instruments(NQ))
    events = list(provider.events())
    assert len(events) == 1
    status = provider.status
    assert status.events_received == 3
    assert status.events_accepted == 1
    assert status.events_rejected == 0


def test_unknown_record_shape_is_rejected_not_crashed_on():
    class Mystery:
        pass

    client = FakeClient([mapping_msg(), Mystery(), trade_msg()])
    provider = make_provider(lambda: client)
    provider.connect(make_sub(), make_instruments(NQ))
    events = list(provider.events())
    assert len(events) == 1
    assert provider.status.events_rejected == 1


# -- ErrorMsg handling: fatal vs. non-fatal vs. data-gap ------------------


def test_fatal_error_code_closes_the_stream():
    client = FakeClient([mapping_msg(), error_msg(err="auth failed", code=_FakeCode("AUTH_FAILED"))])
    provider = make_provider(lambda: client)
    provider.connect(make_sub(), make_instruments(NQ))
    with pytest.raises(LiveProviderAuthenticationError):
        list(provider.events())
    assert provider.state is ConnectionState.FAILED


def test_internal_error_code_closes_the_stream():
    """Phase 4 correction §5: INTERNAL_ERROR was missing from the
    originally-delivered fatal-code list."""
    client = FakeClient([mapping_msg(), error_msg(err="internal", code=_FakeCode("INTERNAL_ERROR"))])
    provider = make_provider(lambda: client)
    provider.connect(make_sub(), make_instruments(NQ))
    with pytest.raises(LiveProviderUnavailableError):
        list(provider.events())
    assert provider.state is ConnectionState.FAILED


def test_replay_data_aged_out_closes_the_stream():
    """Phase 4 correction §5: REPLAY_DATA_AGED_OUT was missing from the
    originally-delivered fatal-code list."""
    client = FakeClient([mapping_msg(), error_msg(err="aged out", code=_FakeCode("REPLAY_DATA_AGED_OUT"))])
    provider = make_provider(lambda: client)
    provider.connect(make_sub(), make_instruments(NQ))
    with pytest.raises(LiveProviderUnavailableError):
        list(provider.events())
    assert provider.state is ConnectionState.FAILED


def test_non_fatal_error_code_does_not_close_the_stream():
    client = FakeClient([
        mapping_msg(),
        error_msg(err="symbol resolution failed", code=_FakeCode("SYMBOL_RESOLUTION_FAILED")),
        trade_msg(),
    ])
    provider = make_provider(lambda: client)
    provider.connect(make_sub(), make_instruments(NQ))
    events = list(provider.events())
    assert len(events) == 1
    assert provider.status.events_rejected == 1  # the error record itself


def test_error_with_unrecognized_code_is_treated_as_non_fatal():
    client = FakeClient([mapping_msg(), error_msg(err="mystery", code=None), trade_msg()])
    provider = make_provider(lambda: client)
    provider.connect(make_sub(), make_instruments(NQ))
    events = list(provider.events())
    assert len(events) == 1


def test_connection_limit_exceeded_translates_to_rate_limit_error():
    client = FakeClient([mapping_msg(), error_msg(err="limit", code=_FakeCode("CONNECTION_LIMIT_EXCEEDED"))])
    provider = make_provider(lambda: client)
    provider.connect(make_sub(), make_instruments(NQ))
    with pytest.raises(LiveProviderRateLimitError):
        list(provider.events())


def test_invalid_subscription_error_translates_to_permission_error():
    client = FakeClient([mapping_msg(), error_msg(err="bad sub", code=_FakeCode("INVALID_SUBSCRIPTION"))])
    provider = make_provider(lambda: client)
    provider.connect(make_sub(), make_instruments(NQ))
    with pytest.raises(LiveProviderPermissionError):
        list(provider.events())


def test_skipped_records_error_transitions_to_degraded_then_recovers():
    """Phase 4 correction §5: SKIPPED_RECORDS_AFTER_SLOW_READING is not
    session-fatal, but represents genuine, documented market-data loss
    -- Olive must surface it explicitly (DEGRADED + data_gap_count),
    never silently continue as though the stream stayed fully healthy.
    A subsequent, successfully-accepted event proves the stream
    recovered, clearing DEGRADED back to CONNECTED."""
    client = FakeClient([
        mapping_msg(),
        error_msg(err="slow reader", code=_FakeCode("SKIPPED_RECORDS_AFTER_SLOW_READING")),
        trade_msg(),
    ])
    provider = make_provider(lambda: client)
    provider.connect(make_sub(), make_instruments(NQ))
    events_iter = provider.events()
    trade = next(events_iter)
    assert isinstance(trade, LiveTrade)
    assert provider.status.data_gap_count == 1
    assert provider.state is ConnectionState.CONNECTED


def test_skipped_records_error_without_recovery_leaves_gap_counted():
    client = FakeClient([mapping_msg(), error_msg(err="slow reader", code=_FakeCode("SKIPPED_RECORDS_AFTER_SLOW_READING"))])
    provider = make_provider(lambda: client)
    provider.connect(make_sub(), make_instruments(NQ))
    events = list(provider.events())
    assert events == []
    assert provider.status.data_gap_count == 1
    # the stream then drained cleanly (no further records) -- a clean
    # end always reports STOPPED, which is correct: the connection
    # ended, it did not stay degraded forever.
    assert provider.state is ConnectionState.STOPPED


# -- Liveness/staleness telemetry -----------------------------------------


def test_status_counters_track_received_accepted_rejected_correctly():
    client = FakeClient([
        mapping_msg(),
        trade_msg(price="21000.25"),  # accepted
        trade_msg(price="21000.10"),  # rejected: tick misaligned
        system_msg(),  # neither
    ])
    provider = make_provider(lambda: client)
    provider.connect(make_sub(), make_instruments(NQ))
    list(provider.events())
    status = provider.status
    assert status.events_received == 4
    assert status.events_accepted == 1
    assert status.events_rejected == 1
    assert status.events_accepted + status.events_rejected <= status.events_received


def test_is_stale_false_immediately_after_a_fresh_event():
    client = FakeClient([mapping_msg(), trade_msg()])
    provider = make_provider(lambda: client, stale_threshold_seconds=Decimal("3600"))
    provider.connect(make_sub(), make_instruments(NQ))
    list(provider.events())
    assert provider.status.is_stale is False


def test_is_stale_true_when_last_receive_exceeds_threshold():
    # A FULLY DRAINED generator ends the stream (STOPPED), and a
    # stopped stream is never reported as "stale" (it was deliberately
    # closed, not silently starved) -- so this test only partially
    # drains the generator, leaving the provider CONNECTED with a
    # receive timestamp that can then age past the threshold.
    client = FakeClient([mapping_msg(), trade_msg()])
    provider = make_provider(lambda: client, stale_threshold_seconds=Decimal("0.000001"))
    provider.connect(make_sub(), make_instruments(NQ))
    events = provider.events()
    first_event = next(events)
    assert isinstance(first_event, LiveTrade)
    assert provider.state is ConnectionState.CONNECTED

    import time as _time

    _time.sleep(0.01)
    assert provider.status.is_stale is True


# -- ReconnectPolicy ---------------------------------------------------------


def test_reconnect_policy_backoff_sequence_is_bounded_and_deterministic():
    policy = dbl.ReconnectPolicy(max_attempts=10, base_delay_seconds=Decimal("1"), max_delay_seconds=Decimal("8"))
    delays = [policy.delay_for_attempt(i) for i in range(1, 6)]
    assert delays == [Decimal("1"), Decimal("2"), Decimal("4"), Decimal("8"), Decimal("8")]


@pytest.mark.parametrize("bad_max_attempts", [None, "5", 1.5, True])
def test_reconnect_policy_rejects_malformed_max_attempts(bad_max_attempts):
    with pytest.raises(LiveDataError):
        dbl.ReconnectPolicy(max_attempts=bad_max_attempts, base_delay_seconds=Decimal("1"), max_delay_seconds=Decimal("2"))


def test_reconnect_policy_rejects_negative_max_attempts():
    with pytest.raises(LiveDataError):
        dbl.ReconnectPolicy(max_attempts=-1, base_delay_seconds=Decimal("1"), max_delay_seconds=Decimal("2"))


@pytest.mark.parametrize("field_name", ["base_delay_seconds", "max_delay_seconds"])
@pytest.mark.parametrize("bad_value", [None, "1", 1, True, Decimal("0"), Decimal("-1"), Decimal("NaN")])
def test_reconnect_policy_rejects_malformed_delay_fields(field_name, bad_value):
    kwargs = dict(max_attempts=3, base_delay_seconds=Decimal("1"), max_delay_seconds=Decimal("5"))
    kwargs[field_name] = bad_value
    with pytest.raises(LiveDataError):
        dbl.ReconnectPolicy(**kwargs)


def test_reconnect_policy_rejects_max_delay_below_base_delay():
    with pytest.raises(LiveDataError):
        dbl.ReconnectPolicy(max_attempts=3, base_delay_seconds=Decimal("10"), max_delay_seconds=Decimal("5"))


def test_reconnect_policy_delay_for_attempt_rejects_zero_or_negative_attempt():
    policy = dbl.ReconnectPolicy(max_attempts=3, base_delay_seconds=Decimal("1"), max_delay_seconds=Decimal("5"))
    with pytest.raises(LiveDataError):
        policy.delay_for_attempt(0)
    with pytest.raises(LiveDataError):
        policy.delay_for_attempt(-1)


def test_reconnect_policy_delay_for_attempt_rejects_bool():
    policy = dbl.ReconnectPolicy(max_attempts=3, base_delay_seconds=Decimal("1"), max_delay_seconds=Decimal("5"))
    with pytest.raises(LiveDataError):
        policy.delay_for_attempt(True)


def test_default_reconnect_policy_used_when_none_supplied():
    provider = make_provider(lambda: FakeClient([]))
    # Exercised indirectly: a provider with no injected reconnect_policy
    # must still construct successfully and connect.
    provider.connect(make_sub(), make_instruments(NQ))
    assert provider.state is ConnectionState.CONNECTED


# -- Real-package introspection (Phase 4 correction §9) -------------------


def test_real_databento_live_and_historical_constructors_match_this_adapters_assumptions():
    """When the real 'databento' package IS installed in this
    environment, confirm -- via inspect.signature, with NO network and
    NO API key -- that Live's constructor genuinely has no 'dataset'
    parameter and that Historical's constructor accepts 'key'. This is
    a genuinely optional, environment-dependent supplementary check
    (explicitly called for by Phase 4 correction §9), not a substitute
    for any of the deterministic fake-based tests above: it is skipped
    outright (never silently passed) when the real package is absent,
    via pytest.importorskip -- distinct from the environment-dependent
    pytest.skip anti-pattern Phase 4 correction §1 removed from the
    Phase 3 historical-provider tests, which was about simulating
    absence for a test that could otherwise always run against a fake.
    This check has no fake equivalent: its entire point is to inspect
    the REAL package's own declared signature."""
    databento = pytest.importorskip("databento")

    live_params = inspect.signature(databento.Live.__init__).parameters
    assert "dataset" not in live_params, (
        "The real databento.Live constructor now accepts 'dataset' -- this adapter's "
        "corrected assumption (Phase 4 correction §2) that dataset belongs only in "
        "subscribe() no longer matches the installed package and must be re-reviewed."
    )

    historical_params = inspect.signature(databento.Historical.__init__).parameters
    assert "key" in historical_params


# -- Real-package record-class introspection (Phase 4.2 correction §2) ---
#
# The constructor-signature check above only confirms Live/Historical's
# own construction shape. It says nothing about whether the actual
# record classes this adapter's normalization logic duck-types against
# (TradeMsg, MBP1Msg, OHLCVMsg, SymbolMappingMsg, ErrorMsg, SystemMsg,
# BidAskPair) still expose the attribute names Olive assumes -- which is
# exactly the kind of DBN/API shape mismatch a real-package upgrade
# could silently introduce. These checks require NO API key and make NO
# network call: they only inspect the class objects themselves (via
# `dir()`), which is always safe offline. Every one of these tests is
# skipped outright (never silently passed) when the real package is
# absent, via pytest.importorskip -- Olive's own logic is independently,
# deterministically covered by the fake-based tests throughout the rest
# of this file regardless of whether this environment has databento
# installed.

_REQUIRED_REAL_RECORD_CLASS_ATTRS = {
    "TradeMsg": ("instrument_id", "ts_event", "ts_recv", "price", "size", "sequence"),
    "MBP1Msg": ("instrument_id", "ts_event", "ts_recv", "sequence"),
    "OHLCVMsg": ("instrument_id", "ts_event", "open", "high", "low", "close", "volume"),
    "SymbolMappingMsg": ("instrument_id", "stype_in_symbol", "stype_out_symbol"),
    "ErrorMsg": ("code",),
    "SystemMsg": ("code",),
    "BidAskPair": ("bid_px", "ask_px", "bid_sz", "ask_sz"),
}


def _locate_real_record_class(databento_module, name):
    """Locate a real Databento record class by name: check the
    top-level ``databento`` package first, then fall back to the
    lower-level ``databento_dbn`` package that ``databento`` itself is
    built on and re-exports record types from. Returns ``None`` only if
    neither package exposes the name -- the caller treats that as a
    genuine, loudly-reported compatibility break, never a silent skip,
    since the caller only runs once ``databento`` itself is confirmed
    importable."""
    cls = getattr(databento_module, name, None)
    if cls is not None:
        return cls
    try:
        import databento_dbn
    except ImportError:
        return None
    return getattr(databento_dbn, name, None)


@pytest.mark.parametrize("class_name,expected_attrs", sorted(_REQUIRED_REAL_RECORD_CLASS_ATTRS.items()))
def test_real_databento_record_classes_expose_the_attributes_this_adapter_assumes(class_name, expected_attrs):
    """Phase 4.2 correction §2: beyond the Live/Historical
    constructor-signature check above, when the real
    databento/databento_dbn packages ARE installed, offline
    (no network, no API key) introspection of the real record classes
    themselves must confirm Olive's assumed attribute names are still
    valid on the actual installed version -- catching an API/DBN shape
    mismatch before it ever reaches a live connection on the user's
    machine."""
    databento = pytest.importorskip("databento")
    cls = _locate_real_record_class(databento, class_name)
    assert cls is not None, (
        f"Neither databento.{class_name} nor databento_dbn.{class_name} could be located -- "
        "this adapter's assumption about where this record class lives no longer matches the "
        "installed package(s) and must be re-reviewed."
    )
    present = dir(cls)
    missing = [attr for attr in expected_attrs if attr not in present]
    assert not missing, (
        f"databento's real {class_name} is missing expected attribute(s) {missing} -- this "
        "adapter's normalization logic assumes they exist and must be re-reviewed against the "
        "installed package version."
    )


def test_real_databento_mbp1_exposes_a_top_of_book_representation_this_adapter_can_read():
    """Specifically confirms the real MBP1Msg exposes AT LEAST ONE of
    the two top-of-book representations app.data.providers.databento_live's
    _mbp1_top_of_book_fields() helper knows how to read: the flat
    bid_px_00/ask_px_00/bid_sz_00/ask_sz_00 properties, and/or a
    'levels' attribute (whose [0] entry is expected to expose
    bid_px/ask_px/bid_sz/ask_sz -- covered by the BidAskPair check
    above). If the real package exposes neither, this adapter's entire
    MBP-1 quote-extraction strategy (Phase 4.2 correction §1) no longer
    matches the installed package and must be re-reviewed."""
    databento = pytest.importorskip("databento")
    cls = _locate_real_record_class(databento, "MBP1Msg")
    assert cls is not None
    present = dir(cls)
    has_flat = any(attr in present for attr in dbl._MBP1_FLAT_TOP_OF_BOOK_ATTRS)
    has_levels = "levels" in present
    assert has_flat or has_levels, (
        "The real MBP1Msg exposes neither the flat bid_px_00/ask_px_00/bid_sz_00/ask_sz_00 "
        "top-of-book properties nor a 'levels' attribute -- this adapter's MBP-1 quote "
        "extraction (Phase 4.2 correction §1) no longer matches the installed package."
    )


def test_real_dbn_records_normalize_offline_with_exact_prices_and_timestamps():
    """Exercise real DBN records through public streaming with fake transport only."""
    db = pytest.importorskip("databento")
    ts = 1_700_000_000_000_000_000
    scale = db.FIXED_PRICE_SCALE
    assert Decimal(scale) == dbl._PRICE_FIXED_POINT_SCALE
    mapping = db.SymbolMappingMsg(
        publisher_id=1, instrument_id=12345, ts_event=ts,
        stype_in=db.SType.RAW_SYMBOL, stype_in_symbol=NQ.display_code,
        stype_out=db.SType.INSTRUMENT_ID, stype_out_symbol="12345",
        start_ts=ts, end_ts=ts + 1_000_000_000,
    )
    common = dict(publisher_id=1, instrument_id=12345, ts_event=ts,
                  price=21000 * scale + scale // 4, size=2,
                  action=db.Action.TRADE, side=db.Side.BID, depth=0, ts_recv=ts + 2_000_000,
                  sequence=42)
    trade = db.TradeMsg(**common)
    quote = db.MBP1Msg(**common, levels=db.BidAskPair(
        bid_px=21000 * scale, ask_px=21000 * scale + scale // 4,
        bid_sz=3, ask_sz=5))
    empty_bid = db.MBP1Msg(**common, levels=db.BidAskPair(
        bid_px=db.UNDEF_PRICE, ask_px=21000 * scale + scale // 4,
        bid_sz=0, ask_sz=5))
    bar = db.OHLCVMsg(
        rtype=db.RType.OHLCV_1S, publisher_id=1, instrument_id=12345,
        ts_event=ts, open=21000 * scale, high=21001 * scale,
        low=20999 * scale, close=21000 * scale + scale // 2, volume=10)
    heartbeat = db.SystemMsg(ts_event=ts, msg="heartbeat", code=db.SystemCode.HEARTBEAT)
    client = FakeClient([mapping, heartbeat, trade, quote, empty_bid, bar])
    provider = make_provider(lambda: client)
    provider.connect(make_sub(event_types=tuple(LiveEventType)), make_instruments(NQ))
    try:
        events = list(provider.events())
        assert len(events) == 4
        t, q, empty, b = events
        assert isinstance(t, LiveTrade) and t.price == Decimal("21000.25")
        assert t.size == 2 and t.sequence == 42
        assert (t.ts_recv - t.ts_event).total_seconds() == 0.002
        assert isinstance(q, LiveQuote)
        assert (q.bid_price, q.ask_price, q.bid_size, q.ask_size) == (
            Decimal("21000"), Decimal("21000.25"), 3, 5)
        assert empty.bid_price is None and empty.bid_size is None
        assert empty.ask_price == Decimal("21000.25")
        assert isinstance(b, LiveBar)
        assert (b.open, b.high, b.low, b.close, b.volume) == (
            Decimal("21000"), Decimal("21001"), Decimal("20999"), Decimal("21000.5"), 10)
        for event in events:
            assert event.contract == NQ
            assert event.provider_instrument_id == "12345"
            assert event.olive_received_at is not None
        assert provider.status.events_rejected == 0
    finally:
        provider.close()
    assert client.stopped


def test_real_dbn_data_loss_and_fatal_errors_are_not_silently_accepted():
    db = pytest.importorskip("databento")
    ts = 1_700_000_000_000_000_000
    gap = db.ErrorMsg(ts_event=ts, err="records lost", is_last=False,
                      code=db.ErrorCode.SKIPPED_RECORDS_AFTER_SLOW_READING)
    client = FakeClient([gap])
    provider = make_provider(lambda: client)
    provider.connect(make_sub(), make_instruments(NQ))
    try:
        assert list(provider.events()) == []
        assert provider.status.data_gap_count == 1
    finally:
        provider.close()
    fatal = db.ErrorMsg(ts_event=ts, err="credential must not escape", is_last=True,
                        code=db.ErrorCode.AUTH_FAILED)
    provider = make_provider(lambda: FakeClient([fatal]))
    provider.connect(make_sub(), make_instruments(NQ))
    try:
        with pytest.raises(LiveProviderAuthenticationError) as caught:
            list(provider.events())
        assert "credential must not escape" not in str(caught.value)
    finally:
        provider.close()
