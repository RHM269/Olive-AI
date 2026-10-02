"""Olive AI futures domain package.

Phase 2 teaches Olive what its tradable futures products (NQ, MNQ)
actually are: instrument economics, contract identity, the quarterly
expiration/roll calendar, and the regular CME Globex session schedule.

This package intentionally knows nothing about market prices, data
providers, strategies, or predictions -- see docs/futures_domain.md
for the full scope and known limitations.

Importing this package (or any of its submodules) performs no I/O,
no network calls, and loads no configuration file -- that only
happens when a loader function such as ``load_instrument_registry()``
or ``load_cycle_date_calendar()`` is explicitly called.
"""
