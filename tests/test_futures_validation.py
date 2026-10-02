"""Tests for app.futures.validation: Olive PRODUCTION domain integrity.

These are distinct from app.futures.{models,registry,calendar}'s own
structural-validity tests: every fixture here is deliberately
STRUCTURALLY VALID (it would pass FuturesInstrument/
FuturesInstrumentRegistry/CycleDateCalendar's own checks without
complaint) but factually wrong about what Olive actually requires in
production. That gap -- found by a fourth external audit -- is exactly
what app.futures.validation exists to close.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.futures.calendar import CycleDateCalendar, load_cycle_date_calendar
from app.futures.models import (
    ContractCycleDates,
    CycleDateSource,
    FuturesDomainError,
    FuturesInstrument,
    InvalidRegistryError,
    SettlementType,
)
from app.futures.registry import FuturesInstrumentRegistry, load_instrument_registry
from app.futures.validation import (
    OLIVE_REQUIRED_OFFICIAL_CYCLE_DATES,
    REQUIRED_BASELINE_YEARS,
    REQUIRED_TRADABLE_ROOTS,
    ProductionDomainIntegrityError,
    _NQ_SPEC,
    _validate_instrument_matches_spec,
    validate_olive_cycle_calendar,
    validate_olive_futures_domain,
    validate_olive_tradable_registry,
)


@pytest.fixture(scope="module")
def real_registry():
    return load_instrument_registry()


@pytest.fixture(scope="module")
def real_calendar():
    return load_cycle_date_calendar()


def _nq(**overrides) -> FuturesInstrument:
    defaults = dict(
        root_symbol="NQ",
        display_name="E-mini Nasdaq-100 Futures",
        exchange="CME",
        underlying="Nasdaq-100 Index",
        currency="USD",
        multiplier=Decimal("20"),
        tick_size=Decimal("0.25"),
        tick_value=Decimal("5.00"),
        settlement_type=SettlementType.CASH,
        contract_months=(3, 6, 9, 12),
    )
    defaults.update(overrides)
    return FuturesInstrument(**defaults)


def _mnq(**overrides) -> FuturesInstrument:
    defaults = dict(
        root_symbol="MNQ",
        display_name="Micro E-mini Nasdaq-100 Futures",
        exchange="CME",
        underlying="Nasdaq-100 Index",
        currency="USD",
        multiplier=Decimal("2"),
        tick_size=Decimal("0.25"),
        tick_value=Decimal("0.50"),
        settlement_type=SettlementType.CASH,
        contract_months=(3, 6, 9, 12),
    )
    defaults.update(overrides)
    return FuturesInstrument(**defaults)


# -- Real production config must pass ----------------------------------------


def test_real_registry_passes_production_validation(real_registry):
    validate_olive_tradable_registry(real_registry)  # must not raise


def test_real_calendar_passes_production_validation(real_calendar):
    validate_olive_cycle_calendar(real_calendar)  # must not raise


def test_real_domain_passes_combined_validation(real_registry, real_calendar):
    validate_olive_futures_domain(real_registry, real_calendar)  # must not raise


# -- The exact external-audit failure case: wrong-but-self-consistent NQ -----


def test_exact_external_audit_nq_corruption_rejected(real_registry):
    """The exact reproduction: NQ multiplier=2, tick_size=0.25,
    tick_value=0.50, contract_months=[3] -- internally self-consistent
    (0.25 * 2 == 0.50) so FuturesInstrument itself accepts it, and the
    registry still contains exactly {NQ, MNQ} -- but this must still be
    rejected by production validation."""
    wrong_nq = _nq(multiplier=Decimal("2"), tick_size=Decimal("0.25"), tick_value=Decimal("0.50"), contract_months=(3,))
    mnq = real_registry.get("MNQ")
    bad_registry = FuturesInstrumentRegistry({"NQ": wrong_nq, "MNQ": mnq})

    with pytest.raises(ProductionDomainIntegrityError):
        validate_olive_tradable_registry(bad_registry)


def test_exact_external_audit_nq_corruption_structurally_valid_on_its_own():
    """Sanity guard on the premise of the test above: the corrupted NQ
    object must NOT raise at the generic FuturesInstrument level --
    otherwise this wouldn't be exercising the production-validation gap
    at all."""
    wrong_nq = _nq(multiplier=Decimal("2"), tick_size=Decimal("0.25"), tick_value=Decimal("0.50"), contract_months=(3,))
    assert wrong_nq.multiplier == Decimal("2")


# -- Individual NQ spec corruption --------------------------------------------


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("multiplier", Decimal("2")),
        ("tick_size", Decimal("0.50")),
        ("tick_value", Decimal("10.00")),
        ("exchange", "ICE"),
        ("currency", "EUR"),
        ("underlying", "S&P 500 Index"),
    ],
)
def test_nq_field_corruption_rejected(real_registry, field, bad_value):
    mnq = real_registry.get("MNQ")
    overrides = {field: bad_value}
    # Keep the object self-consistent (tick_value == tick_size *
    # multiplier) so FuturesInstrument's OWN generic validation still
    # accepts it -- the point is to exercise PRODUCTION validation, not
    # trip the generic structural check instead.
    if field == "tick_size":
        overrides["tick_value"] = Decimal("20") * bad_value
    elif field == "multiplier":
        overrides["tick_value"] = bad_value * Decimal("0.25")
    elif field == "tick_value":
        overrides["multiplier"] = bad_value / Decimal("0.25")
    wrong_nq = _nq(**overrides)
    bad_registry = FuturesInstrumentRegistry({"NQ": wrong_nq, "MNQ": mnq})

    with pytest.raises(ProductionDomainIntegrityError):
        validate_olive_tradable_registry(bad_registry)


class _FakeInstrumentWithSettlement:
    """A duck-typed stand-in exposing only the attributes
    ``_validate_instrument_matches_spec`` reads, used to exercise the
    ``settlement_type`` mismatch path directly. :class:`SettlementType`
    currently has exactly one member (``CASH``), so no *real*
    :class:`FuturesInstrument` can be constructed with a different,
    still-valid settlement type -- this duck-typed object is the only
    way to exercise that one comparison without inventing a second,
    fictitious settlement type in the production enum itself.
    """

    def __init__(self, **overrides):
        self.root_symbol = "NQ"
        self.exchange = "CME"
        self.underlying = "Nasdaq-100 Index"
        self.currency = "USD"
        self.multiplier = Decimal("20")
        self.tick_size = Decimal("0.25")
        self.tick_value = Decimal("5.00")
        self.settlement_type = "PHYSICAL"  # deliberately not SettlementType.CASH
        self.contract_months = (3, 6, 9, 12)
        self.__dict__.update(overrides)


def test_nq_settlement_type_corruption_rejected():
    fake = _FakeInstrumentWithSettlement()

    with pytest.raises(ProductionDomainIntegrityError):
        _validate_instrument_matches_spec(fake, _NQ_SPEC)


@pytest.mark.parametrize(
    "months",
    [
        (3,),  # the exact external case: March only
        (3, 6, 9),  # missing December
        (6, 9, 12),  # missing March
    ],
)
def test_nq_quarterly_months_corruption_rejected(real_registry, months):
    # Note: Olive's quarterly month universe is exactly {3, 6, 9, 12}, so
    # an "unexpected extra" quarterly month cannot occur once an
    # instrument already passes FuturesInstrument's own structural
    # validation (which rejects non-quarterly months outright) -- the
    # only reachable corruption is a strict SUBSET missing one or more
    # of the four required months, which is what these cases cover.
    mnq = real_registry.get("MNQ")
    wrong_nq = _nq(contract_months=months)
    bad_registry = FuturesInstrumentRegistry({"NQ": wrong_nq, "MNQ": mnq})

    with pytest.raises(ProductionDomainIntegrityError):
        validate_olive_tradable_registry(bad_registry)


# -- MNQ corruption (representative, not exhaustive) --------------------------


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("multiplier", Decimal("20")),
        ("tick_value", Decimal("5.00")),
        ("currency", "GBP"),
    ],
)
def test_mnq_field_corruption_rejected(real_registry, field, bad_value):
    nq = real_registry.get("NQ")
    overrides = {field: bad_value}
    if field == "multiplier":
        overrides["tick_value"] = bad_value * Decimal("0.25")
    elif field == "tick_value":
        overrides["multiplier"] = bad_value / Decimal("0.25")
    wrong_mnq = _mnq(**overrides)
    bad_registry = FuturesInstrumentRegistry({"NQ": nq, "MNQ": wrong_mnq})

    with pytest.raises(ProductionDomainIntegrityError):
        validate_olive_tradable_registry(bad_registry)


def test_mnq_quarterly_months_corruption_rejected(real_registry):
    nq = real_registry.get("NQ")
    wrong_mnq = _mnq(contract_months=(3, 6))
    bad_registry = FuturesInstrumentRegistry({"NQ": nq, "MNQ": wrong_mnq})

    with pytest.raises(ProductionDomainIntegrityError):
        validate_olive_tradable_registry(bad_registry)


# -- Tradable-root-set checks (preserved from app.health, now here) ----------


def test_unexpected_extra_tradable_root_rejected(real_registry):
    es = FuturesInstrument(
        root_symbol="ES",
        display_name="E-mini S&P 500 Futures",
        exchange="CME",
        underlying="S&P 500 Index",
        currency="USD",
        multiplier=Decimal("50"),
        tick_size=Decimal("0.25"),
        tick_value=Decimal("12.50"),
        settlement_type=SettlementType.CASH,
        contract_months=(3, 6, 9, 12),
    )
    registry = FuturesInstrumentRegistry(
        {"NQ": real_registry.get("NQ"), "MNQ": real_registry.get("MNQ"), "ES": es}
    )

    with pytest.raises(ProductionDomainIntegrityError):
        validate_olive_tradable_registry(registry)


def test_missing_tradable_root_rejected(real_registry):
    registry = FuturesInstrumentRegistry({"NQ": real_registry.get("NQ")})

    with pytest.raises(ProductionDomainIntegrityError):
        validate_olive_tradable_registry(registry)


def test_required_tradable_roots_is_exactly_nq_mnq():
    assert REQUIRED_TRADABLE_ROOTS == frozenset({"NQ", "MNQ"})


# -- Official calendar baseline -----------------------------------------------

_REAL_DATES = dict(OLIVE_REQUIRED_OFFICIAL_CYCLE_DATES)


def _build_full_calendar(overrides: dict | None = None, years=(2025, 2026, 2027, 2028)) -> CycleDateCalendar:
    overrides = overrides or {}
    official = {}
    for (year, month), (expiration, roll) in _REAL_DATES.items():
        if year not in years:
            continue
        if (year, month) in overrides:
            expiration, roll = overrides[(year, month)]
        official[(year, month)] = ContractCycleDates(
            year=year, month=month, expiration=expiration, roll=roll, source=CycleDateSource.OFFICIAL
        )
    return CycleDateCalendar(
        official_dates=official,
        as_of="2026-10-01",
        covered_years=tuple(years),
        source="test fixture",
    )


def test_full_valid_baseline_calendar_passes():
    cal = _build_full_calendar()
    validate_olive_cycle_calendar(cal)  # must not raise


def test_calendar_truncated_to_2025_only_rejected():
    """The exact external-audit reproduction: a calendar containing only
    complete, structurally valid 2025 quarterly entries. Generically
    valid (covered_years matches what's actually there), but Olive's
    required 2026-2028 baseline is gone."""
    cal = _build_full_calendar(years=(2025,))

    with pytest.raises(ProductionDomainIntegrityError):
        validate_olive_cycle_calendar(cal)


def test_calendar_missing_one_required_year_rejected():
    cal = _build_full_calendar(years=(2025, 2026, 2027))  # 2028 missing

    with pytest.raises(ProductionDomainIntegrityError):
        validate_olive_cycle_calendar(cal)


def test_calendar_with_future_years_added_still_passes():
    """A legitimate future update that ADDS a source-backed year beyond
    2028 must not break production validation -- the baseline check is
    a subset check, not an exact-set check."""
    official = {}
    for (year, month), (expiration, roll) in _REAL_DATES.items():
        official[(year, month)] = ContractCycleDates(
            year=year, month=month, expiration=expiration, roll=roll, source=CycleDateSource.OFFICIAL
        )
    for month, (expiration, roll) in (
        (3, (date(2029, 3, 16), date(2029, 3, 12))),
        (6, (date(2029, 6, 15), date(2029, 6, 11))),
        (9, (date(2029, 9, 21), date(2029, 9, 17))),
        (12, (date(2029, 12, 21), date(2029, 12, 17))),
    ):
        official[(2029, month)] = ContractCycleDates(
            year=2029, month=month, expiration=expiration, roll=roll, source=CycleDateSource.OFFICIAL
        )
    cal = CycleDateCalendar(
        official_dates=official,
        as_of="2029-01-01",
        covered_years=(2025, 2026, 2027, 2028, 2029),
        source="test fixture with future year added",
    )

    validate_olive_cycle_calendar(cal)  # must not raise


def test_required_baseline_years_matches_2025_through_2028():
    assert REQUIRED_BASELINE_YEARS == frozenset({2025, 2026, 2027, 2028})


# -- June 2026 anchor protection (and representative other 2026 anchors) -----


def test_june_2026_anchor_corrupted_to_nominal_friday_rejected():
    """June 2026's official expiration (2026-06-18, a Thursday -- the
    headline Phase 2 holiday exception) silently replaced by the naive
    nominal third Friday (2026-06-19). The roll invariant is anchored to
    the nominal Friday regardless of expiration, so this remains
    constructible as a structurally valid ContractCycleDates -- it must
    still be rejected by production validation as a corrupted anchor."""
    cal = _build_full_calendar(overrides={(2026, 6): (date(2026, 6, 19), date(2026, 6, 15))})

    with pytest.raises(ProductionDomainIntegrityError):
        validate_olive_cycle_calendar(cal)


def test_june_2026_anchor_demoted_to_nominal_source_rejected():
    """June 2026 (and, necessarily, the rest of calendar year 2026 --
    CycleDateCalendar's own cross-validation requires full quarterly
    OFFICIAL coverage for every claimed year, so a single row can't be
    dropped in isolation while still claiming 2026) removed from the
    OFFICIAL table entirely. Equivalent in effect to June 2026 falling
    back to CALCULATED_NOMINAL: the required anchor is simply gone.
    Must still be rejected by production validation even though the
    resulting calendar is itself perfectly structurally valid."""
    cal = _build_full_calendar(years=(2025, 2027, 2028))  # 2026 dropped entirely

    with pytest.raises(ProductionDomainIntegrityError):
        validate_olive_cycle_calendar(cal)


@pytest.mark.parametrize(
    "year,month,bad_expiration,real_roll",
    [
        (2026, 3, date(2026, 3, 21), date(2026, 3, 16)),  # one day later than the real 2026-03-20
        (2026, 9, date(2026, 9, 21), date(2026, 9, 14)),  # one day later than the real 2026-09-18
        (2026, 12, date(2026, 12, 21), date(2026, 12, 14)),  # one day later than the real 2026-12-18
    ],
)
def test_other_2026_anchor_corruption_rejected(year, month, bad_expiration, real_roll):
    # Only `expiration` is a free parameter here: `roll` must exactly
    # equal nominal_customary_roll(nominal_third_friday(year, month)) or
    # ContractCycleDates.__post_init__ itself refuses to construct the
    # object at all -- so a "wrong roll, correct expiration" fixture
    # isn't reachable as a structurally valid OFFICIAL row in the first
    # place. These cases corrupt expiration instead, keeping the one
    # real, invariant-satisfying roll value.
    cal = _build_full_calendar(overrides={(year, month): (bad_expiration, real_roll)})

    with pytest.raises(ProductionDomainIntegrityError):
        validate_olive_cycle_calendar(cal)


def test_correct_june_2026_anchor_passes():
    cal = _build_full_calendar()
    assert cal.get(2026, 6).expiration == date(2026, 6, 18)
    assert cal.get(2026, 6).roll == date(2026, 6, 15)

    validate_olive_cycle_calendar(cal)  # must not raise


# -- Health-system end-to-end (the exact external failure case) --------------


def test_health_reports_error_for_corrupted_nq_registry(real_registry, monkeypatch):
    import app.health as health_module
    from app.config import load_settings
    from app.health import HealthState, get_system_health

    wrong_nq = _nq(multiplier=Decimal("2"), tick_size=Decimal("0.25"), tick_value=Decimal("0.50"), contract_months=(3,))
    bad_registry = FuturesInstrumentRegistry({"NQ": wrong_nq, "MNQ": real_registry.get("MNQ")})
    monkeypatch.setattr(health_module, "load_instrument_registry", lambda: bad_registry)

    settings = load_settings(load_dotenv_file=False)
    health = get_system_health(settings)
    futures_domain = health.component("Futures domain")

    assert futures_domain is not None
    assert futures_domain.state is HealthState.ERROR
    assert "NQ" in futures_domain.detail


def test_health_reports_error_for_truncated_calendar(monkeypatch):
    import app.health as health_module
    from app.config import load_settings
    from app.health import HealthState, get_system_health

    truncated = _build_full_calendar(years=(2025,))
    monkeypatch.setattr(health_module, "load_cycle_date_calendar", lambda: truncated)

    settings = load_settings(load_dotenv_file=False)
    health = get_system_health(settings)
    futures_domain = health.component("Futures domain")

    assert futures_domain is not None
    assert futures_domain.state is HealthState.ERROR


def test_health_still_reports_configured_for_real_config():
    from app.config import load_settings
    from app.health import HealthState, get_system_health

    settings = load_settings(load_dotenv_file=False)
    health = get_system_health(settings)
    futures_domain = health.component("Futures domain")

    assert futures_domain is not None
    assert futures_domain.state is HealthState.CONFIGURED
    assert "NQ" in futures_domain.detail
    assert "MNQ" in futures_domain.detail


# -- Generic loaders must remain generic --------------------------------------


def test_generic_registry_still_accepts_non_olive_instrument():
    """FuturesInstrumentRegistry itself must remain capable of holding a
    completely unrelated, validly-configured instrument -- the strict
    NQ/MNQ rule belongs to validate_olive_tradable_registry, not to the
    generic registry class itself."""
    es = FuturesInstrument(
        root_symbol="ES",
        display_name="E-mini S&P 500 Futures",
        exchange="CME",
        underlying="S&P 500 Index",
        currency="USD",
        multiplier=Decimal("50"),
        tick_size=Decimal("0.25"),
        tick_value=Decimal("12.50"),
        settlement_type=SettlementType.CASH,
        contract_months=(3, 6, 9, 12),
    )
    registry = FuturesInstrumentRegistry({"ES": es})

    assert registry.contains("ES")
    # Production validation correctly rejects it (wrong universe
    # entirely) -- but the GENERIC registry class itself never did.
    with pytest.raises(ProductionDomainIntegrityError):
        validate_olive_tradable_registry(registry)


def test_generic_calendar_still_accepts_an_unrelated_single_year():
    """CycleDateCalendar itself must remain capable of representing a
    complete, self-consistent calendar for a year range that isn't
    Olive's required baseline -- the strict 2025-2028 rule belongs to
    validate_olive_cycle_calendar, not to the generic calendar class."""
    cal = _build_full_calendar(years=(2027,))

    assert cal.covered_years == (2027,)
    with pytest.raises(ProductionDomainIntegrityError):
        validate_olive_cycle_calendar(cal)


# ============================================================================
# Phase 2.5 adversarial regression tests: the three production validators'
# own parameters were never validated. An internal QA process correction
# (triggered by a fifth external audit) found that
# validate_olive_tradable_registry / validate_olive_cycle_calendar /
# validate_olive_futures_domain leaked a raw AttributeError for any
# malformed or wrong-kind argument -- exactly the defect class Phase 2.3
# established must never happen at a public futures-domain boundary,
# repeated one correction later in a brand-new module. Fixed via the
# shared _require_registry (app.futures.registry) and the already-existing
# _require_cycle_calendar (app.futures.calendar) validators.
# ============================================================================

_MALFORMED_PARAMETER_VALUES = (None, True, False, "bad", 123, 1.5, [], {}, object())


@pytest.mark.parametrize("bad_value", _MALFORMED_PARAMETER_VALUES, ids=repr)
def test_validate_olive_tradable_registry_rejects_malformed_values(bad_value):
    with pytest.raises(FuturesDomainError):
        validate_olive_tradable_registry(bad_value)


@pytest.mark.parametrize("bad_value", _MALFORMED_PARAMETER_VALUES, ids=repr)
def test_validate_olive_cycle_calendar_rejects_malformed_values(bad_value):
    with pytest.raises(FuturesDomainError):
        validate_olive_cycle_calendar(bad_value)


def test_validate_olive_tradable_registry_rejects_wrong_domain_object(real_calendar):
    """A *different* Olive domain object (not a primitive) must still be
    rejected with a domain error, not an AttributeError -- this is the
    specific case the external reviewer's primitive-only sweep did not
    cover, and the one that makes an hasattr()-based fix insufficient."""
    with pytest.raises(InvalidRegistryError):
        validate_olive_tradable_registry(real_calendar)


def test_validate_olive_cycle_calendar_rejects_wrong_domain_object(real_registry):
    with pytest.raises(FuturesDomainError):
        validate_olive_cycle_calendar(real_registry)


def test_validate_olive_futures_domain_rejects_swapped_arguments(real_registry, real_calendar):
    """Calling validate_olive_futures_domain with its two arguments
    swapped (an easy caller mistake since both parameters are
    positional-friendly) must fail with a domain error, not an
    AttributeError, and without needing any duplicated type check
    inside validate_olive_futures_domain itself."""
    with pytest.raises(FuturesDomainError):
        validate_olive_futures_domain(real_calendar, real_registry)


@pytest.mark.parametrize("bad_registry", _MALFORMED_PARAMETER_VALUES, ids=repr)
def test_validate_olive_futures_domain_rejects_malformed_registry(bad_registry, real_calendar):
    with pytest.raises(FuturesDomainError):
        validate_olive_futures_domain(bad_registry, real_calendar)


@pytest.mark.parametrize("bad_calendar", _MALFORMED_PARAMETER_VALUES, ids=repr)
def test_validate_olive_futures_domain_rejects_malformed_calendar(bad_calendar, real_registry):
    with pytest.raises(FuturesDomainError):
        validate_olive_futures_domain(real_registry, bad_calendar)


class _DuckTypedFakeRegistry:
    """Exposes .roots and .get(...) but is not a FuturesInstrumentRegistry.

    Phase 2.4's original health-layer adversarial tests used exactly this
    shape of fake (see the pre-Phase-2.5 tests/test_health.py). The
    self-audit that produced Phase 2.5 explicitly called out that a
    hasattr()-style fix would still let an object like this through --
    this fixture's .get() raises if ever called, so a passing test here
    proves the rejection happens via a genuine isinstance() check, before
    any of this object's attributes/methods are trusted.
    """

    def __init__(self, roots: tuple[str, ...]) -> None:
        self.roots = roots

    def get(self, root_symbol: str) -> FuturesInstrument:
        raise AssertionError("a rejected fake registry must never be queried for instruments")


class _DuckTypedFakeCalendar:
    """Exposes .covered_years, .has_official(...), and .get(...) but is
    not a CycleDateCalendar. See _DuckTypedFakeRegistry above."""

    def __init__(self, years: tuple[int, ...]) -> None:
        self.covered_years = years

    def has_official(self, year: int, month: int) -> bool:
        raise AssertionError("a rejected fake calendar must never be queried for official coverage")

    def get(self, year: int, month: int) -> ContractCycleDates:
        raise AssertionError("a rejected fake calendar must never be queried for cycle dates")


def test_validate_olive_tradable_registry_rejects_duck_typed_fake():
    fake = _DuckTypedFakeRegistry(("NQ", "MNQ"))
    with pytest.raises(InvalidRegistryError):
        validate_olive_tradable_registry(fake)


def test_validate_olive_cycle_calendar_rejects_duck_typed_fake():
    fake = _DuckTypedFakeCalendar((2025, 2026, 2027, 2028))
    with pytest.raises(FuturesDomainError):
        validate_olive_cycle_calendar(fake)


def test_validate_olive_futures_domain_rejects_duck_typed_fakes():
    fake_registry = _DuckTypedFakeRegistry(("NQ", "MNQ"))
    fake_calendar = _DuckTypedFakeCalendar((2025, 2026, 2027, 2028))
    with pytest.raises(FuturesDomainError):
        validate_olive_futures_domain(fake_registry, fake_calendar)


def test_real_domain_still_passes_after_parameter_hardening(real_registry, real_calendar):
    """Positive-path regression: the Phase 2.5 parameter validation must
    not have disturbed the real, correctly-typed production path."""
    validate_olive_tradable_registry(real_registry)
    validate_olive_cycle_calendar(real_calendar)
    validate_olive_futures_domain(real_registry, real_calendar)


def test_malformed_registry_error_never_leaks_as_attribute_error():
    """Directly pins the exact failure mode the external audit reported:
    the exception type itself must be a FuturesDomainError, not an
    AttributeError, TypeError, or any other raw stdlib exception."""
    try:
        validate_olive_tradable_registry("bad")
    except FuturesDomainError:
        pass
    else:
        pytest.fail("expected validate_olive_tradable_registry('bad') to raise")


def test_malformed_calendar_error_never_leaks_as_attribute_error():
    try:
        validate_olive_cycle_calendar("bad")
    except FuturesDomainError:
        pass
    else:
        pytest.fail("expected validate_olive_cycle_calendar('bad') to raise")


def test_health_reports_error_not_crash_for_malformed_registry_dependency(monkeypatch):
    """End-to-end regression for the health/system-state consequence the
    self-audit identified: app.health._check_futures_domain() only
    catches FuturesDomainError. Before Phase 2.5, a malformed object
    reaching validate_olive_futures_domain from a future caller would
    have propagated as an unhandled AttributeError all the way out of
    get_system_health() instead of being reported as ERROR. This proves
    that can no longer happen for the health entry point itself."""
    import app.health as health_module
    from app.config import load_settings
    from app.health import HealthState, get_system_health

    monkeypatch.setattr(health_module, "load_instrument_registry", lambda: "not a registry")

    settings = load_settings(load_dotenv_file=False)
    health = get_system_health(settings)  # must not raise
    futures_domain = health.component("Futures domain")

    assert futures_domain is not None
    assert futures_domain.state is HealthState.ERROR
    assert "FuturesInstrumentRegistry" in futures_domain.detail
