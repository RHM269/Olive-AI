# Olive AI — Architecture (Phase 1)

This document describes Olive AI's architecture as it exists at the end
of **Phase 1 — Foundation**. It intentionally does not describe future
phases' internals; see the project's phase plan for what comes next.

## Scope of Phase 1

Phase 1 establishes the application shell: configuration, logging,
health reporting, an entry point, and a test suite. It implements no
market-data, futures-domain, feature, strategy, prediction, signal,
backtesting, paper-trading, alerting, or web functionality. Those are
explicitly out of scope until their respective phases.

## Module Map

```
app/
├── __init__.py        Package marker only. No side effects on import.
├── config.py           Typed Settings, Environment, DataMode, load_settings()
├── logging_config.py   configure_logging(), get_logger()
└── health.py            HealthState, ComponentHealth, SystemHealth,
                          get_system_health(), format_health_report()

main.py                  Orchestration-only entry point
tests/                    pytest suite covering the above
```

## Startup / Data Flow

```
Environment Variables (+ optional .env)
            |
            v
      app.config.load_settings()
            |
            v
         Settings  ------------------->  app.logging_config.configure_logging()
            |                                        |
            v                                        v
  app.health.get_system_health()                 Logger ("olive")
            |
            v
  app.health.format_health_report()
            |
            v
         main.py  ---->  stdout status report, exit code
```

1. `main.py` calls `load_settings()`, which reads `APP_NAME`,
   `ENVIRONMENT`, `LOG_LEVEL`, and `OLIVE_DATA_MODE` from the process
   environment (optionally pre-populated from a local `.env` file),
   falling back to safe development defaults for anything unset. A
   value that is set but invalid raises `ConfigurationError`, and
   `main.py` fails closed (prints an error, exits `1`) rather than
   guessing.
2. `configure_logging()` sets up Olive's `"olive"` logger once per
   process, using the resolved log level. Calling it again (e.g. in
   tests, or if `main()` runs twice) updates the level without
   attaching a second handler.
3. `get_system_health()` builds an immutable, timestamped
   `SystemHealth` snapshot: `System` and `Configuration` are reported
   as real (`ONLINE` / `CONFIGURED`), and every other subsystem is
   reported as `NOT_IMPLEMENTED` with a note on which future phase
   will build it.
4. `format_health_report()` turns that structured snapshot into the
   CLI text `main.py` prints. This formatting step is deliberately
   separate from health computation so a future API or Olive Web page
   can reuse `SystemHealth` without re-deriving or re-parsing text.

## Health Semantics

`HealthState` distinguishes:

- `NOT_IMPLEMENTED` -- the component does not exist in the codebase
  yet (true for every subsystem but System/Configuration in Phase 1).
- `NOT_CONFIGURED` -- the component exists but is missing required
  configuration (not used yet in Phase 1, but reserved for e.g. a
  Phase 3+ data provider with no API key).
- `ONLINE` / `CONFIGURED` -- genuinely operational today.
- `DEGRADED` / `ERROR` -- reserved for future runtime-failure
  reporting.

This vocabulary is intentionally small and is expected to be reused,
not replaced, by later phases.

## Testing

- `tests/test_config.py` -- defaults, environment overrides,
  invalid-value handling, and immutability of `Settings`. Every test
  clears Olive's environment variables via `monkeypatch` and calls
  `load_settings(load_dotenv_file=False)` so results never depend on a
  developer's local `.env`.
- `tests/test_health.py` -- confirms `System`/`Configuration` report
  as operational, confirms every other Phase 1 component is honestly
  reported as not yet built, checks timestamp timezone-awareness,
  component-name uniqueness, and structured (non-string-only) output.
- `tests/test_main.py` -- startup smoke tests: successful run, fail-
  closed behavior on invalid configuration, and no duplicate log
  handlers across repeated `main()` calls.

## Architectural Boundaries (must hold for all future phases)

- **`main.py`** -- orchestration only. No trading, data, or model
  logic belongs here.
- **`app/config.py`** -- configuration only. No business logic.
- **`app/health.py`** -- status reporting only. No live calls to
  market-data providers or any other external service.
- **`app/logging_config.py`** -- logging setup only.

Future phases add new domain packages under `app/` (e.g. `futures/`,
`data/`, `features/`, `strategies/`, `prediction/`, `signals/`) rather
than growing these four modules into general-purpose dumping grounds,
and rather than placing that logic in web routes or `main.py`.

## Extension Philosophy

Phase 1 deliberately does not create empty placeholder packages for
Phases 2-14 (`futures/`, `data/`, `realtime/`, `features/`,
`strategies/`, `prediction/`, `signals/`, `events/`, `backtest/`,
`paper/`, `alerts/`, `web/`). Each is created when its phase actually
begins, with real contents, tests, and documentation -- not as
speculative scaffolding.

The two extension points Phase 1 guarantees are:

1. `get_system_health()` can flip any entry in `_NOT_YET_IMPLEMENTED`
   over to a real, operational `ComponentHealth` (or introduce
   `NOT_CONFIGURED` states) as each subsystem is actually built,
   without changing the `SystemHealth` / `ComponentHealth` contract.
2. `Settings` can grow new typed fields (and `DataMode` can gain real
   meaning beyond `development`) as later phases need them, without
   breaking `load_settings()`'s no-secrets-required guarantee for
   local development.
