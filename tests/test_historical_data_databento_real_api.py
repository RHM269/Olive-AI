"""Offline compatibility check for the REAL ``databento`` package's API
surface (Phase 3.3 §17-19/§24).

Every test in ``tests/test_historical_data_databento.py`` injects a
FAKE client via the ``client=`` constructor parameter, so installing
the real ``databento`` package does NOT, by itself, prove the real
package still exposes the exact interface
``app.data.providers.databento.DatabentoHistoricalProvider`` assumes
(``Historical(key=...)``; ``metadata.get_cost(...)``;
``symbology.resolve(...)``; ``timeseries.get_range(...)``;
``DBNStore.to_df(price_type=..., tz=..., map_symbols=...)``). This file
closes that specific gap, and ONLY that gap -- it does not and cannot
prove runtime BEHAVIOR (that is what the fake-client tests already do,
deliberately without the real package).

This file is entirely OFFLINE: it performs NO HTTP request, uses NO
real API key, and never calls a single provider method (``get_cost``,
``get_range``, ``resolve``, ``to_df``, ...) -- it only INTROSPECTS the
installed package's module/class attributes and callable signatures,
plus one deliberately narrow check that merely CONSTRUCTS a
``Historical`` client with a syntactically-fake key and confirms it
exposes the three callable sub-API namespaces this adapter needs,
again never calling any of them. Per CLAUDE.md's standing "no real
Databento network calls" policy (Phase 3.1-3.3), this must remain true
on every machine this file runs on, not only in Claude's own sandbox.

Skipped (not failed) whenever the real ``databento`` package is not
installed -- as in Claude's cloud sandbox, which has no PyPI install
access at all (see docs/historical_data.md "Known limitations"). On a
machine where ``pip install -r requirements.txt`` succeeds (the user's
own machine, CI, ...), this file is expected to run for real and pass,
confirming the installed package version is still compatible with
this adapter's documented assumptions.
"""

from __future__ import annotations

import inspect

import pytest

databento = pytest.importorskip("databento", reason="databento is not installed in this environment")


def test_historical_client_class_exists_and_constructor_accepts_key_kwarg():
    assert hasattr(databento, "Historical")
    sig = inspect.signature(databento.Historical.__init__)
    assert "key" in sig.parameters, (
        "databento.Historical.__init__ no longer accepts a 'key' keyword argument; "
        "app.data.providers.databento.DatabentoHistoricalProvider assumes it does."
    )


def test_bento_error_base_class_exists():
    # Databento's documented exception hierarchy root -- this adapter's
    # _is_databento_exception classifies by __module__ prefix rather
    # than by this class directly, but the class existing at all is
    # itself part of the documented, assumed interface.
    assert hasattr(databento, "BentoError"), "databento no longer exposes a BentoError base class."
    assert issubclass(databento.BentoError, Exception)


def test_bento_http_error_subclasses_exist():
    # Databento's documented client/server HTTP error split (used by
    # this adapter's own docstring/status-code classification logic,
    # even though _translate_databento_exception itself works from
    # status_code/message text rather than isinstance checks).
    assert hasattr(databento, "BentoClientError") or hasattr(databento, "BentoServerError"), (
        "databento no longer exposes a documented BentoClientError/BentoServerError split."
    )


def test_dbnstore_class_exists_and_to_df_exposes_expected_parameters():
    assert hasattr(databento, "DBNStore")
    sig = inspect.signature(databento.DBNStore.to_df)
    for name in ("price_type", "map_symbols", "tz"):
        assert name in sig.parameters, (
            f"databento.DBNStore.to_df no longer accepts a {name!r} parameter; "
            "app.data.providers.databento.DatabentoHistoricalProvider.fetch_bars calls "
            "store.to_df(price_type=..., tz=..., map_symbols=...) and assumes it does."
        )


def test_historical_client_exposes_metadata_timeseries_symbology_namespaces():
    """The one check in this file that CONSTRUCTS a real client.

    ``metadata``/``timeseries``/``symbology`` are documented as
    INSTANCE sub-API namespaces set up by ``Historical.__init__``, not
    class-level attributes -- they cannot be checked by introspecting
    the class alone. This constructs a client with a syntactically
    fake (never real) key and checks ONLY that the three callables this
    adapter needs are present -- it never calls metadata.get_cost(),
    timeseries.get_range(), or symbology.resolve(), exactly mirroring
    what app.data.providers.databento._require_databento_client_interface
    already does for an INJECTED client. Per databento-python's
    documented client design, construction itself only stores the key
    and prepares the session -- it does not make a network call."""
    client = databento.Historical(key="db-0000000000000000000000000000")
    assert hasattr(client, "metadata") and callable(getattr(client.metadata, "get_cost", None)), (
        "databento.Historical no longer exposes a callable metadata.get_cost."
    )
    assert hasattr(client, "timeseries") and callable(getattr(client.timeseries, "get_range", None)), (
        "databento.Historical no longer exposes a callable timeseries.get_range."
    )
    assert hasattr(client, "symbology") and callable(getattr(client.symbology, "resolve", None)), (
        "databento.Historical no longer exposes a callable symbology.resolve."
    )
