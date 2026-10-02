# Olive AI — Project Instructions for Claude

Olive AI is a Hudson Intelligence product: an AI-powered Nasdaq-100 futures
(NQ/MNQ) market-intelligence and trade-signal platform, developed in 14
sequential phases (see `docs/architecture.md`). Phase 1 (foundation) and
Phase 2 (futures domain, hardened through corrective passes 2.1–2.4) are
complete. Current status and capabilities: see `README.md`.

Standing rules that apply regardless of which phase is in progress:

- Phases are sequential. Do not implement scope from a later phase while
  working on an earlier one, even if it would be convenient (no market
  data, providers, prices, bars, strategies, ML, signals, backtesting,
  paper trading, web UI, or database work until the phase that is actually
  scoped for it).
- Keep system/data-state labels truthful at all times: `LIVE`, `DELAYED`,
  `HISTORICAL`, `SIMULATED`, `DEMO`, `NOT_CONFIGURED`, `STALE`,
  `CONFIGURED`, `VALID`, `HEALTHY`, etc. must never be reported unless the
  underlying evidence actually supports that state. Fail closed rather
  than fabricate a fallback value to preserve a green status.
- Never commit secrets. `.env` and other local secret files stay
  git-ignored.
- Do not commit or push unless the user explicitly authorizes it in that
  turn. Always inspect `git status` truthfully and report it.
- Do not weaken, delete, or water down an existing test merely to make it
  pass. Fix the underlying code instead, or raise the discrepancy.
- GitHub (the user's own machine/repo) is the source of truth; this cloud
  workspace delivers corrective work as a ZIP, not via commits made here.

## Standing QA / self-review instruction (applies to every phase and every corrective revision)

This was established as a permanent policy, not a one-off request — apply
it in full every time, without being asked again.

**Before declaring any implementation complete**, go beyond the explicit
happy-path requirements and beyond the tests already written. Perform an
adversarial engineering review of your own work, as if another senior
engineer were deliberately trying to break it before it reaches
production.

For every public API, domain object, loader, service, and boundary
created or modified, deliberately test (where applicable):

- malformed input types; `None`; booleans where integers are expected;
  empty and whitespace strings; invalid enum values; zero and negative
  values where inappropriate; extreme values; `NaN`/`Infinity` wherever
  numeric input exists; incorrect timezone/date/datetime types; boundary
  timestamps; mismatched objects from different instruments/providers/
  models; duplicate identifiers; inconsistent metadata; direct object
  construction that bypasses loaders; corrupted, partial, or
  unexpected-extra configuration; missing files; stale state; failure
  paths; year/month boundaries; session boundaries; rollover boundaries;
  serialization/deserialization round trips where relevant.

Do not assume Python type hints enforce runtime invariants. Do not assume
a loader's validation makes the underlying domain object safe when it is
constructed directly instead. Expected malformed caller input must fail
with an appropriate Olive/domain-specific error (a subclass of
`FuturesDomainError`, or the equivalent for whatever subsystem is being
built) — never a leaked raw `AttributeError`, `KeyError`, `TypeError`,
`ValueError`, `decimal.InvalidOperation`, `OverflowError`, or similar. Do
not achieve this by broadly swallowing exceptions.

Before considering anything complete, ask:

1. Can this object be constructed in an impossible state?
2. Can two valid objects be combined in an invalid way?
3. Can a caller bypass validation through another code path?
4. Can malformed numeric values enter the system?
5. Can a type hint be violated at runtime?
6. Can boundary dates/times create an off-by-one error?
7. Can corrupted configuration still produce a healthy system state?
8. Can this component falsely report LIVE / CONFIGURED / VALID?
9. Can an unexpected dependency failure produce misleading output?
10. Can a structurally valid but factually wrong configuration still pass?
11. Can a future update accidentally weaken a previously validated
    invariant?
12. What inputs would an external reviewer use specifically to break this
    implementation?

Add meaningful regression tests for anything discovered this way. Do not
inflate the test count with trivial assertions — correctness and
invariant protection matter more than the number of tests.

### Generic structural validity vs. Olive production correctness

Always keep these two questions separate, and know which one a given
check answers (this distinction is precedented in the codebase already —
see `docs/futures_domain.md` §9 and `app/futures/validation.py`, added in
Phase 2.4 for exactly this reason):

- *Generic structural validity*: "Is this object/config internally
  self-consistent?" (e.g. `tick_value == tick_size * multiplier`). A
  generic domain object, calendar, provider response, or loaded model can
  satisfy this while still being wrong for Olive's actual purposes.
- *Olive production correctness*: "Is this specifically the real,
  intended NQ/MNQ product / the required official calendar coverage / the
  freshly-labeled correctly-sourced data / the correct validated model
  version Olive actually requires in production?" Never let "internally
  consistent" be mistaken for "correct for Olive production" — keep the
  generic domain objects/loaders generic, and put the Olive-specific
  requirement in its own, separate, reusable validation layer (as
  `app/futures/validation.py` does for the futures domain).

### Financial/market domain QA (for any future market-data, signal, prediction, or backtesting work)

When that work begins, deliberately test: stale timestamps; duplicate
ticks/bars; out-of-order events; missing events; invalid prices; negative
volume; incorrect contracts; contract-rollover boundaries; closed-market
timestamps; holiday/special-session assumptions; timezone/DST boundaries;
provider partial failure; delayed data mislabeled as live; simulated data
mislabeled as live; future leakage; look-ahead bias; inconsistent model
versions; invalid probability distributions; predictions older than
their horizon; stale signals; event-risk windows; conflicting strategies;
missing features; model-drift states; impossible P&L/fill assumptions.

### Full regression requirement before completion of any phase or correction

1. Run the full existing regression suite.
2. Run all new tests.
3. Run manual adversarial verification.
4. Inspect the final diff.
5. Inspect runtime/system-health output.
6. Inspect package contents.
7. Confirm no future-phase scope leaked in.
8. Confirm no secrets or local environment artifacts are packaged.
9. Confirm documentation matches the actual implementation.
10. Confirm previously fixed bugs remain fixed.

### Final self-review pass before delivery

Before returning a ZIP, a completion report, or declaring a phase
complete, do a separate pass that intentionally tries to disprove "this
is ready" rather than just confirming "I satisfied the prompt." Ask: what
did the prompt forget to ask? What assumptions were made? What would
another senior engineer attack? What could pass all the tests and still
be wrong? What configuration could be self-consistent but factually
incorrect? What failure mode could make the system lie about its own
state? If this surfaces a problem: fix it, add a regression test, rerun
the full suite, and only then deliver.
