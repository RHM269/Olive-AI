"""Olive AI historical market-data package (Phase 3).

Teaches Olive how to request, validate, normalize, and durably store
historical NQ/MNQ futures bars -- the raw-fact foundation later phases
(features, strategies, prediction datasets, backtesting, replay) will
build on. See docs/historical_data.md for the full architecture.

This package intentionally knows nothing about live/real-time data,
feature engineering, strategies, predictions, or backtesting -- see
that document for scope and known limitations.

Importing this package (or any of its submodules) performs no I/O, no
network calls, and loads no configuration, provider client, or
provider package (``databento``/``pyarrow`` are imported lazily, only
at the point they are actually needed). Historical network access only
ever happens through an explicit ``HistoricalDataService`` fetch
operation gated by several independent safety checks -- see
``app.data.service`` and docs/historical_data.md "Network opt-in".
"""
