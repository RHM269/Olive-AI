"""Phase 4 tests for app.data.live_provider_base.RealTimeMarketDataProvider
(the ABC) and app.data.providers.unconfigured_live.UnconfiguredLiveProvider
(the safe default implementation).
"""

from __future__ import annotations

import pytest

from app.data.live_models import ConnectionState, LiveProviderNotConfiguredError, LiveStreamStatus
from app.data.live_provider_base import RealTimeMarketDataProvider
from app.data.providers.unconfigured_live import UnconfiguredLiveProvider


def test_abc_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        RealTimeMarketDataProvider()


def test_abc_requires_every_abstract_method_to_be_implemented():
    class Incomplete(RealTimeMarketDataProvider):
        @property
        def name(self):
            return "incomplete"

    with pytest.raises(TypeError):
        Incomplete()


# -- UnconfiguredLiveProvider --------------------------------------------


def test_default_reason_is_non_empty():
    provider = UnconfiguredLiveProvider()
    assert provider.reason
    assert provider.name == "unconfigured"


def test_custom_reason_is_preserved():
    provider = UnconfiguredLiveProvider(reason="DATABENTO_API_KEY is not set.")
    assert provider.reason == "DATABENTO_API_KEY is not set."


@pytest.mark.parametrize("bad_reason", [None, "", "   ", 123, True])
def test_rejects_malformed_reason(bad_reason):
    with pytest.raises(LiveProviderNotConfiguredError):
        UnconfiguredLiveProvider(reason=bad_reason)


def test_state_is_always_disconnected():
    provider = UnconfiguredLiveProvider()
    assert provider.state is ConnectionState.DISCONNECTED


def test_status_reports_zeroed_disconnected_status():
    provider = UnconfiguredLiveProvider()
    status = provider.status
    assert isinstance(status, LiveStreamStatus)
    assert status.state is ConnectionState.DISCONNECTED
    assert status.events_received == 0
    assert status.events_accepted == 0
    assert status.events_rejected == 0
    assert status.is_stale is False


def test_connect_always_raises_with_the_configured_reason():
    provider = UnconfiguredLiveProvider(reason="no provider selected")
    with pytest.raises(LiveProviderNotConfiguredError, match="no provider selected"):
        provider.connect(subscription=object(), instruments={})


def test_events_always_raises_with_the_configured_reason():
    provider = UnconfiguredLiveProvider(reason="no provider selected")
    with pytest.raises(LiveProviderNotConfiguredError, match="no provider selected"):
        provider.events()


def test_close_is_always_safe_and_idempotent():
    provider = UnconfiguredLiveProvider()
    provider.close()
    provider.close()
    provider.close()


def test_connect_never_mutates_state_even_though_it_raises():
    provider = UnconfiguredLiveProvider()
    try:
        provider.connect(subscription=object(), instruments={})
    except LiveProviderNotConfiguredError:
        pass
    assert provider.state is ConnectionState.DISCONNECTED
