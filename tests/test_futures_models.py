"""Tests for app.futures.models: instrument/contract domain objects."""

from __future__ import annotations

from decimal import Decimal

import pytest

from datetime import date

from datetime import datetime

from app.futures.models import (
    CME_QUARTERLY_MONTH_CODES,
    ContractCycleDates,
    ContractMonth,
    CycleDateError,
    CycleDateSource,
    FuturesConfigurationError,
    FuturesContract,
    FuturesDomainError,
    FuturesInstrument,
    InvalidContractError,
    InvalidContractMonthError,
    InvalidDateError,
    InvalidInstrumentError,
    InvalidPointValueError,
    InvalidTickCountError,
    SettlementType,
    TickAlignmentError,
    _require_contract,
    _require_cycle_dates,
    _require_instrument,
    _require_plain_date,
    nominal_customary_roll,
    nominal_third_friday,
)

_QUARTERLY_MONTHS = (
    ContractMonth.MARCH,
    ContractMonth.JUNE,
    ContractMonth.SEPTEMBER,
    ContractMonth.DECEMBER,
)


def _make_instrument(**overrides) -> FuturesInstrument:
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
        contract_months=_QUARTERLY_MONTHS,
    )
    defaults.update(overrides)
    return FuturesInstrument(**defaults)


# -- Instrument specification tests ---------------------------------------------


def test_nq_root_and_economics():
    nq = _make_instrument()

    assert nq.root_symbol == "NQ"
    assert nq.multiplier == Decimal("20")
    assert nq.tick_size == Decimal("0.25")
    assert nq.tick_value == Decimal("5.00")
    assert nq.currency == "USD"
    assert nq.settlement_type is SettlementType.CASH
    assert set(nq.contract_months) == set(_QUARTERLY_MONTHS)


def test_mnq_root_and_economics():
    mnq = _make_instrument(
        root_symbol="MNQ",
        display_name="Micro E-mini Nasdaq-100 Futures",
        multiplier=Decimal("2"),
        tick_value=Decimal("0.50"),
    )

    assert mnq.root_symbol == "MNQ"
    assert mnq.multiplier == Decimal("2")
    assert mnq.tick_size == Decimal("0.25")
    assert mnq.tick_value == Decimal("0.50")


def test_tick_value_must_match_tick_size_times_multiplier():
    with pytest.raises(FuturesConfigurationError):
        _make_instrument(tick_value=Decimal("9.99"))


@pytest.mark.parametrize(
    "field,value",
    [("multiplier", Decimal("0")), ("multiplier", Decimal("-5")), ("tick_size", Decimal("0")), ("tick_size", Decimal("-0.25"))],
)
def test_non_positive_economics_rejected(field, value):
    overrides = {field: value}
    # tick_value would also need adjusting to "match", but these should fail
    # on the non-positive check before the match check is even reached.
    with pytest.raises(FuturesConfigurationError):
        _make_instrument(**overrides)


def test_empty_root_symbol_rejected():
    with pytest.raises(FuturesConfigurationError):
        _make_instrument(root_symbol="")


def test_empty_contract_months_rejected():
    with pytest.raises(FuturesConfigurationError):
        _make_instrument(contract_months=())


def test_duplicate_contract_months_rejected():
    with pytest.raises(FuturesConfigurationError):
        _make_instrument(contract_months=(ContractMonth.MARCH, ContractMonth.MARCH, ContractMonth.JUNE))


# -- Contract month / CME month code tests ---------------------------------------


def test_cme_month_codes():
    assert CME_QUARTERLY_MONTH_CODES[ContractMonth.MARCH] == "H"
    assert CME_QUARTERLY_MONTH_CODES[ContractMonth.JUNE] == "M"
    assert CME_QUARTERLY_MONTH_CODES[ContractMonth.SEPTEMBER] == "U"
    assert CME_QUARTERLY_MONTH_CODES[ContractMonth.DECEMBER] == "Z"


def test_contract_month_numeric_values():
    assert int(ContractMonth.MARCH) == 3
    assert int(ContractMonth.JUNE) == 6
    assert int(ContractMonth.SEPTEMBER) == 9
    assert int(ContractMonth.DECEMBER) == 12


# -- FuturesContract identity / display code tests --------------------------------


def test_contract_identity_is_unambiguous():
    contract = FuturesContract(root_symbol="NQ", year=2026, month=ContractMonth.DECEMBER)

    assert contract.identity == "NQ-2026-12"


@pytest.mark.parametrize(
    "root,year,month,expected_code",
    [
        ("NQ", 2027, ContractMonth.MARCH, "NQH7"),
        ("NQ", 2027, ContractMonth.JUNE, "NQM7"),
        ("NQ", 2027, ContractMonth.SEPTEMBER, "NQU7"),
        ("NQ", 2026, ContractMonth.DECEMBER, "NQZ6"),
        ("MNQ", 2027, ContractMonth.MARCH, "MNQH7"),
        ("MNQ", 2026, ContractMonth.DECEMBER, "MNQZ6"),
    ],
)
def test_contract_display_code_generation(root, year, month, expected_code):
    contract = FuturesContract(root_symbol=root, year=year, month=month)

    assert contract.display_code == expected_code
    assert str(contract) == expected_code


def test_contract_code_uses_last_digit_of_year_only():
    # 2026 and 2036 both end in 6 -- display_code alone is NOT a safe
    # unambiguous identity across decades, only `identity` is.
    c2026 = FuturesContract(root_symbol="NQ", year=2026, month=ContractMonth.DECEMBER)
    c2036 = FuturesContract(root_symbol="NQ", year=2036, month=ContractMonth.DECEMBER)

    assert c2026.display_code == c2036.display_code == "NQZ6"
    assert c2026.identity != c2036.identity


def test_contract_empty_root_symbol_rejected():
    with pytest.raises(FuturesConfigurationError):
        FuturesContract(root_symbol="", year=2026, month=ContractMonth.DECEMBER)


# -- Instrument.quarter_contract() validation --------------------------------------


def test_quarter_contract_builds_expected_contract():
    nq = _make_instrument()

    contract = nq.quarter_contract(2026, ContractMonth.DECEMBER)

    assert contract.root_symbol == "NQ"
    assert contract.year == 2026
    assert contract.month is ContractMonth.DECEMBER


def test_quarter_contract_rejects_unsupported_month():
    nq = _make_instrument(contract_months=(ContractMonth.MARCH, ContractMonth.JUNE))

    with pytest.raises(InvalidContractMonthError):
        nq.quarter_contract(2026, ContractMonth.DECEMBER)


# -- Tick / price / dollar economics -----------------------------------------------


def test_nq_points_to_ticks_and_dollars():
    nq = _make_instrument()

    assert nq.points_to_ticks(Decimal("1.00")) == 4
    assert nq.points_to_dollars(Decimal("1.00")) == Decimal("20")
    assert nq.ticks_to_dollars(1) == Decimal("5.00")
    assert nq.points_to_dollars(Decimal("10")) == Decimal("200")
    assert nq.ticks_to_points(4) == Decimal("1.00")


def test_mnq_points_to_ticks_and_dollars():
    mnq = _make_instrument(root_symbol="MNQ", multiplier=Decimal("2"), tick_value=Decimal("0.50"))

    assert mnq.points_to_ticks(Decimal("1.00")) == 4
    assert mnq.points_to_dollars(Decimal("1.00")) == Decimal("2")
    assert mnq.ticks_to_dollars(1) == Decimal("0.50")
    assert mnq.points_to_dollars(Decimal("10")) == Decimal("20")


def test_points_to_ticks_rejects_misaligned_value():
    nq = _make_instrument()

    with pytest.raises(TickAlignmentError):
        nq.points_to_ticks(Decimal("1.10"))


def test_points_to_ticks_accepts_plain_values():
    # Convenience: callers may pass a plain int/str/float-free literal;
    # internal math still happens via Decimal.
    nq = _make_instrument()

    assert nq.points_to_ticks("0.25") == 1


# ============================================================================
# Phase 2.1 adversarial regression tests (external review findings)
# ============================================================================


# -- Finding: FuturesContract did not enforce its month invariant -----------------


def test_futures_contract_rejects_non_quarterly_month():
    """FuturesContract("NQ", 2026, 1) must raise, not silently produce NQ-2026-01."""
    with pytest.raises(InvalidContractMonthError):
        FuturesContract("NQ", 2026, 1)


@pytest.mark.parametrize("bad_month", [0, 1, 2, 4, 5, 7, 8, 10, 11, 13, -1])
def test_futures_contract_rejects_every_non_quarterly_month(bad_month):
    with pytest.raises(InvalidContractMonthError):
        FuturesContract("NQ", 2026, bad_month)


def test_futures_contract_canonicalizes_valid_raw_integer_month():
    """A valid raw int month (12) is normalized to ContractMonth.DECEMBER,
    and the invariant isinstance(contract.month, ContractMonth) holds."""
    contract = FuturesContract("NQ", 2026, 12)

    assert contract.month is ContractMonth.DECEMBER
    assert isinstance(contract.month, ContractMonth)
    assert contract.display_code == "NQZ6"


def test_futures_contract_month_is_always_contractmonth_instance():
    for month in (3, 6, 9, 12):
        contract = FuturesContract("NQ", 2026, month)
        assert isinstance(contract.month, ContractMonth)


def test_futures_contract_rejects_bool_month():
    with pytest.raises(InvalidContractMonthError):
        FuturesContract("NQ", 2026, True)


# -- Finding: FuturesContract root symbol normalization -----------------------------


@pytest.mark.parametrize("raw_root", ["nq", "Nq", " nq ", "NQ", " NQ"])
def test_futures_contract_root_symbol_is_canonical(raw_root):
    contract = FuturesContract(raw_root, 2026, ContractMonth.DECEMBER)

    assert contract.root_symbol == "NQ"


def test_futures_contract_rejects_empty_root_after_strip():
    with pytest.raises(FuturesConfigurationError):
        FuturesContract("   ", 2026, ContractMonth.DECEMBER)


# -- Finding: FuturesContract year validation ----------------------------------------


def test_futures_contract_rejects_non_integer_year():
    with pytest.raises(FuturesConfigurationError):
        FuturesContract("NQ", "2026", ContractMonth.DECEMBER)


def test_futures_contract_rejects_bool_year():
    with pytest.raises(FuturesConfigurationError):
        FuturesContract("NQ", True, ContractMonth.DECEMBER)


def test_futures_contract_rejects_unreasonable_year():
    with pytest.raises(FuturesConfigurationError):
        FuturesContract("NQ", 10000, ContractMonth.DECEMBER)


# -- Finding: FuturesInstrument direct construction hardening ------------------------


def test_futures_instrument_normalizes_root_symbol():
    instrument = _make_instrument(root_symbol="nq")

    assert instrument.root_symbol == "NQ"


def test_futures_instrument_rejects_blank_display_name():
    with pytest.raises(FuturesConfigurationError):
        _make_instrument(display_name="   ")


def test_futures_instrument_coerces_string_settlement_type():
    instrument = _make_instrument(settlement_type="CASH")

    assert instrument.settlement_type is SettlementType.CASH


def test_futures_instrument_rejects_invalid_settlement_type():
    with pytest.raises(FuturesConfigurationError):
        _make_instrument(settlement_type="BARTER")


def test_futures_instrument_normalizes_raw_int_contract_months():
    instrument = _make_instrument(contract_months=(3, 6, 9, 12))

    assert instrument.contract_months == _QUARTERLY_MONTHS
    assert all(isinstance(m, ContractMonth) for m in instrument.contract_months)


def test_futures_instrument_rejects_non_quarterly_contract_month():
    with pytest.raises(InvalidContractMonthError):
        _make_instrument(contract_months=(3, 6, 9, 1))


# -- Finding: tick-count integer enforcement -----------------------------------------


def test_ticks_to_points_rejects_fractional_ticks():
    nq = _make_instrument()

    with pytest.raises(InvalidTickCountError):
        nq.ticks_to_points(1.5)


def test_ticks_to_dollars_rejects_fractional_ticks():
    nq = _make_instrument()

    with pytest.raises(InvalidTickCountError):
        nq.ticks_to_dollars(1.5)


def test_ticks_to_points_rejects_decimal_fractional_ticks():
    nq = _make_instrument()

    with pytest.raises(InvalidTickCountError):
        nq.ticks_to_points(Decimal("1.5"))


@pytest.mark.parametrize("bad_value", [True, False])
def test_tick_helpers_reject_bool(bad_value):
    nq = _make_instrument()

    with pytest.raises(InvalidTickCountError):
        nq.ticks_to_points(bad_value)
    with pytest.raises(InvalidTickCountError):
        nq.ticks_to_dollars(bad_value)


@pytest.mark.parametrize("ticks", [0, 1, 4, -1, -4])
def test_negative_and_zero_integer_ticks_remain_valid(ticks):
    """The constraint is 'must be an integer,' not 'must be positive' --
    negative ticks are legitimate for signed price/P&L movement."""
    nq = _make_instrument()

    # Should not raise.
    points = nq.ticks_to_points(ticks)
    dollars = nq.ticks_to_dollars(ticks)

    assert points == Decimal("0.25") * ticks
    assert dollars == Decimal("5.00") * ticks


# -- Finding: ContractCycleDates invariant validation --------------------------------


def _valid_cycle_kwargs(**overrides):
    kwargs = dict(
        year=2026,
        month=9,
        expiration=date(2026, 9, 18),
        roll=date(2026, 9, 14),
        source=CycleDateSource.OFFICIAL,
    )
    kwargs.update(overrides)
    return kwargs


def test_contract_cycle_dates_accepts_consistent_data():
    cycle = ContractCycleDates(**_valid_cycle_kwargs())

    assert cycle.expiration == date(2026, 9, 18)


def test_contract_cycle_dates_rejects_expiration_from_a_different_month():
    """The exact adversarial case from external review: a 'September' cycle
    record whose dates are actually in June."""
    with pytest.raises(CycleDateError):
        ContractCycleDates(**_valid_cycle_kwargs(expiration=date(2026, 6, 18), roll=date(2026, 6, 15)))


def test_contract_cycle_dates_rejects_expiration_from_a_different_year():
    with pytest.raises(CycleDateError):
        ContractCycleDates(**_valid_cycle_kwargs(expiration=date(2027, 9, 17)))


def test_contract_cycle_dates_rejects_roll_from_a_different_month():
    with pytest.raises(CycleDateError):
        ContractCycleDates(**_valid_cycle_kwargs(roll=date(2026, 8, 31)))


def test_contract_cycle_dates_rejects_roll_after_expiration():
    with pytest.raises(CycleDateError):
        ContractCycleDates(**_valid_cycle_kwargs(expiration=date(2026, 9, 10), roll=date(2026, 9, 14)))


def test_contract_cycle_dates_rejects_non_monday_roll():
    # 2026-09-15 is a Tuesday.
    with pytest.raises(CycleDateError):
        ContractCycleDates(**_valid_cycle_kwargs(roll=date(2026, 9, 15)))


def test_contract_cycle_dates_does_not_require_friday_expiration():
    """June 2026's real official expiration (2026-06-18) is a Thursday --
    this must remain valid; Phase 2.1 must not impose a simplistic
    'expiration must always be Friday' rule."""
    cycle = ContractCycleDates(
        year=2026,
        month=6,
        expiration=date(2026, 6, 18),  # Thursday
        roll=date(2026, 6, 15),  # Monday
        source=CycleDateSource.OFFICIAL,
    )

    assert cycle.expiration.strftime("%A") == "Thursday"


def test_contract_cycle_dates_rejects_non_quarterly_month():
    with pytest.raises(CycleDateError):
        ContractCycleDates(**_valid_cycle_kwargs(month=1, expiration=date(2026, 1, 16), roll=date(2026, 1, 12)))


def test_contract_cycle_dates_rejects_invalid_source():
    with pytest.raises(CycleDateError):
        ContractCycleDates(**_valid_cycle_kwargs(source="OFFICIAL"))


# ============================================================================
# Phase 2.2 adversarial regression tests (second external audit findings)
# ============================================================================

# -- Non-finite Decimal economics (direct construction) ----------------------


@pytest.mark.parametrize("bad_value", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_instrument_rejects_non_finite_multiplier(bad_value):
    with pytest.raises(FuturesConfigurationError):
        _make_instrument(multiplier=bad_value)


@pytest.mark.parametrize("bad_value", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_instrument_rejects_non_finite_tick_size(bad_value):
    with pytest.raises(FuturesConfigurationError):
        _make_instrument(tick_size=bad_value, tick_value=Decimal("5.00"))


@pytest.mark.parametrize("bad_value", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_instrument_rejects_non_finite_tick_value(bad_value):
    with pytest.raises(FuturesConfigurationError):
        _make_instrument(tick_value=bad_value)


def test_instrument_non_finite_multiplier_never_leaks_decimal_invalid_operation():
    """The exact bug from the second audit: comparing a NaN Decimal with
    `<=` directly raises decimal.InvalidOperation. `.is_finite()` must be
    checked first so this never escapes as a raw stdlib exception."""
    try:
        _make_instrument(multiplier=Decimal("NaN"))
    except FuturesConfigurationError:
        pass
    else:
        pytest.fail("expected FuturesConfigurationError")


def test_instrument_simultaneous_infinite_tick_size_and_value_rejected():
    """The exact second-audit example: multiplier=1, tick_size=Infinity,
    tick_value=Infinity was previously accepted as 'valid' (since
    Infinity * 1 == Infinity, the cross-check even appeared to agree)."""
    with pytest.raises(FuturesConfigurationError):
        _make_instrument(
            multiplier=Decimal("1"),
            tick_size=Decimal("Infinity"),
            tick_value=Decimal("Infinity"),
        )


# -- Point-value coercion (points_to_ticks / points_to_dollars) --------------


@pytest.mark.parametrize(
    "bad_value",
    [float("nan"), float("inf"), float("-inf"), Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")],
)
def test_points_to_ticks_rejects_non_finite_values(bad_value):
    nq = _make_instrument()
    with pytest.raises(InvalidPointValueError):
        nq.points_to_ticks(bad_value)


@pytest.mark.parametrize(
    "bad_value",
    [float("nan"), float("inf"), float("-inf"), Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")],
)
def test_points_to_dollars_rejects_non_finite_values(bad_value):
    nq = _make_instrument()
    with pytest.raises(InvalidPointValueError):
        nq.points_to_dollars(bad_value)


@pytest.mark.parametrize("bad_value", [True, False, None, "banana"])
def test_points_to_ticks_rejects_non_numeric_values(bad_value):
    nq = _make_instrument()
    with pytest.raises(InvalidPointValueError):
        nq.points_to_ticks(bad_value)


@pytest.mark.parametrize("bad_value", [True, False, None, "banana"])
def test_points_to_dollars_rejects_non_numeric_values(bad_value):
    nq = _make_instrument()
    with pytest.raises(InvalidPointValueError):
        nq.points_to_dollars(bad_value)


@pytest.mark.parametrize("value", [Decimal("0.25"), 1, 1.25, "0.25", -10])
def test_points_to_dollars_still_accepts_legitimate_numeric_convenience_types(value):
    """Preserve the pre-existing caller convenience of passing a Decimal,
    int, float, or numeric string -- only non-finite/non-numeric values
    are newly rejected."""
    nq = _make_instrument()
    result = nq.points_to_dollars(value)
    assert result == Decimal(str(value)) * nq.multiplier


def test_points_to_ticks_alignment_behavior_preserved():
    nq = _make_instrument()
    with pytest.raises(TickAlignmentError):
        nq.points_to_ticks(Decimal("1.10"))
    assert nq.points_to_ticks(Decimal("1.00")) == 4


# -- nominal_third_friday / nominal_customary_roll (now in models.py) -------


def test_nominal_third_friday_rejects_bool_year():
    with pytest.raises(FuturesConfigurationError):
        nominal_third_friday(True, 3)


def test_nominal_third_friday_rejects_string_year():
    with pytest.raises(FuturesConfigurationError):
        nominal_third_friday("2026", 3)


def test_nominal_third_friday_rejects_non_quarterly_month():
    with pytest.raises(CycleDateError):
        nominal_third_friday(2026, 1)


def test_nominal_customary_roll_rejects_official_holiday_shifted_date():
    """The exact second-audit misuse case: passing June 2026's OFFICIAL
    Thursday expiration (not the nominal Friday) must be rejected, not
    silently produce a Sunday 'roll date'."""
    with pytest.raises(CycleDateError):
        nominal_customary_roll(date(2026, 6, 18))


def test_nominal_customary_roll_rejects_non_friday():
    with pytest.raises(CycleDateError):
        nominal_customary_roll(date(2026, 9, 17))  # a Thursday


def test_nominal_customary_roll_accepts_nominal_friday():
    assert nominal_customary_roll(date(2026, 9, 18)) == date(2026, 9, 14)
    assert nominal_customary_roll(date(2026, 6, 19)) == date(2026, 6, 15)


# -- ContractCycleDates: exact customary-roll invariant ----------------------


def test_contract_cycle_dates_rejects_wrong_customary_roll_monday():
    """The exact second-audit example: September 2026 with roll=2026-09-07
    (a real Monday before expiration) must still be rejected, because it
    is not the CUSTOMARY roll Monday for September 2026's nominal third
    Friday."""
    with pytest.raises(CycleDateError):
        ContractCycleDates(**_valid_cycle_kwargs(roll=date(2026, 9, 7)))


def test_contract_cycle_dates_accepts_exact_customary_roll_september_2026():
    cycle = ContractCycleDates(**_valid_cycle_kwargs(roll=date(2026, 9, 14)))
    assert cycle.roll == date(2026, 9, 14)


def test_contract_cycle_dates_accepts_exact_customary_roll_june_2026_holiday_exception():
    """June 2026 must remain valid end-to-end: OFFICIAL expiration
    2026-06-18 (Thursday), customary roll 2026-06-15 (Monday), computed
    from the NOMINAL third Friday (2026-06-19), not from the shifted
    official Thursday."""
    cycle = ContractCycleDates(
        year=2026,
        month=6,
        expiration=date(2026, 6, 18),
        roll=date(2026, 6, 15),
        source=CycleDateSource.OFFICIAL,
    )
    assert cycle.expiration == date(2026, 6, 18)
    assert cycle.roll == date(2026, 6, 15)


def test_contract_cycle_dates_june_2026_nominal_friday_unchanged_by_roll_hardening():
    """Regression guard: the underlying nominal-Friday calculation that
    the stronger roll invariant depends on must still disagree with the
    OFFICIAL expiration for June 2026 (that disagreement IS the headline
    Phase 2 holiday exception) -- Phase 2.2 must not have accidentally
    collapsed nominal and official back together."""
    assert nominal_third_friday(2026, 6) == date(2026, 6, 19)


# ============================================================================
# Phase 2.3 adversarial regression tests (third external audit findings)
# ============================================================================

# -- date-vs-datetime invariant (_require_plain_date) -------------------------


def test_nominal_customary_roll_rejects_datetime():
    """The exact third-audit bug: nominal_customary_roll(datetime(...))
    previously silently returned a wrong-typed datetime result instead of
    rejecting the datetime outright."""
    with pytest.raises(InvalidDateError):
        nominal_customary_roll(datetime(2026, 9, 18, 12))


def test_contract_cycle_dates_rejects_datetime_expiration():
    """The exact third-audit bug: a datetime expiration previously leaked a
    raw TypeError from a cross-type date/datetime comparison."""
    with pytest.raises(InvalidDateError):
        ContractCycleDates(**_valid_cycle_kwargs(expiration=datetime(2026, 9, 18)))


def test_contract_cycle_dates_rejects_datetime_roll():
    with pytest.raises(InvalidDateError):
        ContractCycleDates(**_valid_cycle_kwargs(roll=datetime(2026, 9, 14)))


@pytest.mark.parametrize("bad_value", [None, "2026-09-18", 20260918, True])
def test_require_plain_date_rejects_non_date_values(bad_value):
    with pytest.raises(InvalidDateError):
        _require_plain_date(bad_value, context="test")


def test_require_plain_date_accepts_plain_date():
    d = date(2026, 9, 18)
    assert _require_plain_date(d, context="test") is d


# -- FuturesInstrument.contract_months container validation -------------------


@pytest.mark.parametrize("bad_container", [True, False, 123, "3,6,9,12", b"3,6,9,12", {3, 6, 9, 12}])
def test_instrument_rejects_invalid_contract_months_container(bad_container):
    """The exact third-audit bug: FuturesInstrument(contract_months=True)
    previously leaked a raw TypeError ('bool' object is not iterable) when
    the constructor tried to iterate it."""
    with pytest.raises(FuturesConfigurationError):
        _make_instrument(contract_months=bad_container)


def test_instrument_contract_months_container_rejection_never_leaks_typeerror():
    try:
        _make_instrument(contract_months=True)
    except FuturesConfigurationError:
        pass
    else:
        pytest.fail("expected FuturesConfigurationError")


def test_instrument_still_accepts_list_and_tuple_contract_months():
    assert _make_instrument(contract_months=[3, 6, 9, 12]).contract_months
    assert _make_instrument(contract_months=(3, 6, 9, 12)).contract_months


# -- Decimal arithmetic overflow hardening ------------------------------------

_EXTREME_DECIMAL = Decimal("1E+999999")
# tick_size (0.25) divides into _EXTREME_DECIMAL without exceeding the
# default context's Emax (the quotient's adjusted exponent stays at
# exactly 999999), so points_to_ticks needs a slightly more extreme value
# to actually overflow on division.
_EXTREME_DECIMAL_FOR_DIVISION = Decimal("1E+1000000")


def test_instrument_construction_rejects_tick_value_overflow():
    """The exact third-audit bug: tick_size * multiplier can overflow the
    active Decimal context even though each operand individually passes
    .is_finite(); this must become a domain error, never a raw
    decimal.Overflow."""
    with pytest.raises(FuturesConfigurationError):
        _make_instrument(
            multiplier=_EXTREME_DECIMAL,
            tick_size=_EXTREME_DECIMAL,
            tick_value=_EXTREME_DECIMAL,
        )


def test_instrument_construction_overflow_never_leaks_decimal_exception():
    import decimal

    try:
        _make_instrument(
            multiplier=_EXTREME_DECIMAL,
            tick_size=_EXTREME_DECIMAL,
            tick_value=_EXTREME_DECIMAL,
        )
    except decimal.DecimalException:
        pytest.fail("a raw decimal.DecimalException leaked out of instrument construction")
    except FuturesConfigurationError:
        pass


def test_points_to_ticks_rejects_extreme_overflowing_value():
    nq = _make_instrument()
    with pytest.raises(InvalidPointValueError):
        nq.points_to_ticks(_EXTREME_DECIMAL_FOR_DIVISION)


def test_points_to_dollars_rejects_extreme_overflowing_value():
    nq = _make_instrument()
    with pytest.raises(InvalidPointValueError):
        nq.points_to_dollars(_EXTREME_DECIMAL)


def test_ticks_to_points_rejects_extreme_overflowing_tick_count():
    """A huge (but still true) integer tick count, multiplied by
    tick_size, must become InvalidTickCountError rather than a raw
    decimal.Overflow. A genuinely astronomical tick count (enough to
    overflow the real Decimal context's Emax=999999) would take the test
    suite far too long to construct/convert, so this temporarily tightens
    the active Decimal context's Emax to exercise the exact same
    try/except DecimalException code path cheaply and deterministically."""
    import decimal

    nq = _make_instrument()
    with decimal.localcontext() as ctx:
        ctx.Emax = 50
        ctx.Emin = -50
        with pytest.raises(InvalidTickCountError):
            nq.ticks_to_points(10**60)


def test_ticks_to_dollars_rejects_extreme_overflowing_tick_count():
    import decimal

    nq = _make_instrument()
    with decimal.localcontext() as ctx:
        ctx.Emax = 50
        ctx.Emin = -50
        with pytest.raises(InvalidTickCountError):
            nq.ticks_to_dollars(10**60)


def test_decimal_overflow_does_not_invent_arbitrary_economic_maximums():
    """Guard against over-fixing: ordinary, realistic economics values must
    remain completely unaffected by the overflow handling."""
    nq = _make_instrument()
    assert nq.points_to_dollars(Decimal("100")) == Decimal("2000")
    assert nq.points_to_ticks(Decimal("1")) == 4
    assert nq.ticks_to_points(4) == Decimal("1.00")
    assert nq.ticks_to_dollars(4) == Decimal("20.00")


# -- Shared public-API object validators --------------------------------------


def test_require_instrument_accepts_instrument():
    nq = _make_instrument()
    assert _require_instrument(nq, context="test") is nq


@pytest.mark.parametrize("bad_value", [None, "NQ", 123, object()])
def test_require_instrument_rejects_non_instrument(bad_value):
    with pytest.raises(InvalidInstrumentError):
        _require_instrument(bad_value, context="test")


def test_require_contract_accepts_contract():
    nq = _make_instrument()
    contract = nq.quarter_contract(2026, 9)
    assert _require_contract(contract, context="test") is contract


@pytest.mark.parametrize("bad_value", [None, "NQZ6", 123, object()])
def test_require_contract_rejects_non_contract(bad_value):
    with pytest.raises(InvalidContractError):
        _require_contract(bad_value, context="test")


def test_require_cycle_dates_accepts_cycle_dates():
    cycle = ContractCycleDates(**_valid_cycle_kwargs())
    assert _require_cycle_dates(cycle, context="test") is cycle


@pytest.mark.parametrize("bad_value", [None, "cycle", 123, object()])
def test_require_cycle_dates_rejects_non_cycle_dates(bad_value):
    with pytest.raises(CycleDateError):
        _require_cycle_dates(bad_value, context="test")
