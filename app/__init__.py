"""Olive AI application package.

This package establishes ``app`` as Olive AI's primary application
namespace. Importing it (or any of its submodules) must remain a safe,
side-effect-free operation: no configuration is loaded, no logging is
configured, and no startup behavior runs merely by importing.

Phase 1 provides only the foundational modules below. Later phases will
add domain packages (futures, data, features, strategies, prediction,
signals, backtest, paper, alerts, web) without modifying this file's
contract.
"""

__version__ = "0.1.0"
