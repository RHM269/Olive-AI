# Olive AI

Olive AI is a Hudson Intelligence product being developed as an AI-powered
Nasdaq-100 futures (**NQ** / **MNQ**) market-intelligence and trade-signal
platform. It is designed around depth on one market rather than breadth
across many, and is intended to eventually produce probabilistic
`LONG` / `SHORT` / `NO_TRADE` theses with explicit uncertainty, not
guaranteed outcomes.

## Current Status

**Phase 1 — Foundation.**

Olive AI currently has **no futures domain knowledge, no market data, no
predictive models, no strategies, no signals, and no web interface.**
This phase exists solely to establish a clean, honest, testable
application foundation that every later phase builds on.

## Long-Term Purpose

Olive AI's eventual goal is to continuously analyze Nasdaq-100 futures
and produce calibrated, explainable `LONG` / `SHORT` / `NO_TRADE`
trade theses -- with entry zones, invalidation levels, targets, and
supporting/opposing evidence -- backed by real feature engineering,
validated machine-learning models, and a signal-fusion engine. It is
developed in 14 sequential phases; see `docs/architecture.md` for how
Phase 1 fits into that plan.

## Current Capabilities

Phase 1 provides only:

- A typed, environment-driven configuration system (`app/config.py`)
- Centralized, duplicate-safe logging setup (`app/logging_config.py`)
- A structured, truthful system-health model (`app/health.py`)
- An application entry point that starts, reports status, and exits
  cleanly (`main.py`)
- A test suite covering configuration, health, and startup behavior

## Not Yet Implemented

The following are intentionally **not** part of Phase 1 and will be
added in later phases:

- Futures contract/instrument engine (expiration, rollover, sessions)
- Historical and real-time market data
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
- Phase 1 makes no network calls of any kind.

## Development Roadmap

Olive AI is built in 14 sequential phases (Foundation, Futures Domain,
Historical Data, Real-Time Data, Market Intelligence, Strategies,
Prediction Engine, Fusion/Signal Engine, Backtesting, Paper Trading,
Olive Web, Advanced Intelligence, Always-On Operation, and Production
Validation). Each phase is implemented, tested, and committed before
the next begins. See `docs/architecture.md` for the Phase 1 details
and extension philosophy.
