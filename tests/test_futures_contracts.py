"""Tests for app.futures.contracts: contract lifecycle & tick/price helpers."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.futures.contracts import (
    next_contract,
    points_to_dollars,
    points_to_ticks,
    previous_contract,
    quarter_contract,
    ticks_to_dollars,
    ticks_to_points,
)
from app.futures.models import (
    ContractInstrumentMismatchError,
    ContractMonth,
    FuturesContract,
    InvalidContractError,
    InvalidContractMonthError,
    InvalidInstrumentError,
    TickAlignmentError,
)
from app.futures.registry import load_instrument_registry


@pytest.fixture(scope="module")
def registry():
    return load_instrument_registry()


@pytest.fixture
def nq(registry):
    return registry.get("NQ")


@pytest.fixture
def mnq(registry):
    return registry.get("MNQ")


# -- next_contract / previous_contract --------------------------------------------


@pytest.mark.parametrize(
    "start_year,start_month,expected_year,expected_month",
    [
        (2026, ContractMonth.SEPTEMBER, 2026, ContractMonth.DECEMBER),
        (2026, ContractMonth.DECEMBER, 2027, ContractMonth.MARCH),  # year rollover
        (2027, ContractMonth.MARCH, 2027, ContractMonth.JUNE),
    ],
)
def test_next_contract(nq, start_year, start_month, expected_year, expected_month):
    start = nq.quarter_contract(start_year, start_month)

    result = next_contract(start, nq)

    assert result.year == expected_year
    assert result.month is expected_month


def test_next_contract_nqu6_to_nqz6(nq):
    nqu6 = quarter_contract(nq, 2026, ContractMonth.SEPTEMBER)

    result = next_contract(nqu6, nq)

    assert result.display_code == "NQZ6"


def test_next_contract_nqz6_to_nqh7_year_rollover(nq):
    nqz6 = quarter_contract(nq, 2026, ContractMonth.DECEMBER)

    result = next_contract(nqz6, nq)

    assert result.display_code == "NQH7"
    assert result.year == 2027
    assert result.month is ContractMonth.MARCH


def test_next_contract_mnq_year_rollover(mnq):
    mnqz6 = quarter_contract(mnq, 2026, ContractMonth.DECEMBER)

    result = next_contract(mnqz6, mnq)

    assert result.display_code == "MNQH7"


@pytest.mark.parametrize(
    "start_year,start_month,expected_year,expected_month",
    [
        (2027, ContractMonth.MARCH, 2026, ContractMonth.DECEMBER),  # year rollback
        (2026, ContractMonth.DECEMBER, 2026, ContractMonth.SEPTEMBER),
    ],
)
def test_previous_contract(nq, start_year, start_month, expected_year, expected_month):
    start = nq.quarter_contract(start_year, start_month)

    result = previous_contract(start, nq)

    assert result.year == expected_year
    assert result.month is expected_month


def test_next_then_previous_is_identity(nq):
    original = quarter_contract(nq, 2026, ContractMonth.SEPTEMBER)

    round_tripped = previous_contract(next_contract(original, nq), nq)

    assert round_tripped == original


# -- quarter_contract() module-level helper ----------------------------------------


def test_quarter_contract_accepts_int_month(nq):
    contract = quarter_contract(nq, 2026, 12)

    assert contract.month is ContractMonth.DECEMBER
    assert contract.display_code == "NQZ6"


# -- Tick / price / dollar helpers (module-level wrappers) -------------------------


def test_nq_economics_helpers(nq):
    assert points_to_ticks(nq, Decimal("1.00")) == 4
    assert ticks_to_points(nq, 4) == Decimal("1.00")
    assert points_to_dollars(nq, Decimal("10")) == Decimal("200")
    assert ticks_to_dollars(nq, 1) == Decimal("5.00")


def test_mnq_economics_helpers(mnq):
    assert points_to_ticks(mnq, Decimal("1.00")) == 4
    assert points_to_dollars(mnq, Decimal("10")) == Decimal("20")
    assert ticks_to_dollars(mnq, 1) == Decimal("0.50")


def test_misaligned_points_raise_via_module_helper(nq):
    with pytest.raises(TickAlignmentError):
        points_to_ticks(nq, Decimal("1.10"))


# ============================================================================
# Phase 2.1 adversarial regression tests (external review findings)
# ============================================================================
#
# These exercise the PUBLIC next_contract()/previous_contract() behavior
# future phases will actually depend upon -- not private helpers directly.


def test_next_contract_rejects_cross_instrument_nq_contract_with_mnq_instrument(nq, mnq):
    """An NQ contract must never be silently advanced "as if" it were MNQ's."""
    nqu6 = quarter_contract(nq, 2026, ContractMonth.SEPTEMBER)

    with pytest.raises(ContractInstrumentMismatchError):
        next_contract(nqu6, mnq)


def test_previous_contract_rejects_cross_instrument_mnq_contract_with_nq_instrument(nq, mnq):
    """Mirror case: an MNQ contract must never be silently stepped back using NQ."""
    mnqz6 = quarter_contract(mnq, 2026, ContractMonth.DECEMBER)

    with pytest.raises(ContractInstrumentMismatchError):
        previous_contract(mnqz6, nq)


def test_next_contract_rejects_contract_month_not_supported_by_instrument():
    """A contract whose root matches the instrument but whose month the
    instrument doesn't actually list as a supported quarterly month must
    raise InvalidContractMonthError -- never a bare ValueError leaked from
    list.index(). Simulated with a same-root instrument definition whose
    contract_months is narrower than the real NQ config's.
    """
    from app.futures.models import FuturesInstrument, SettlementType

    bogus = FuturesContract(root_symbol="NQ", year=2026, month=ContractMonth.MARCH)
    narrow_nq = FuturesInstrument(
        root_symbol="NQ",
        display_name="E-mini Nasdaq-100 (narrow test double)",
        exchange="CME",
        underlying="Nasdaq-100 Index",
        currency="USD",
        multiplier=Decimal("20"),
        tick_size=Decimal("0.25"),
        tick_value=Decimal("5.00"),
        settlement_type=SettlementType.CASH,
        contract_months=(ContractMonth.JUNE, ContractMonth.DECEMBER),
    )

    with pytest.raises(InvalidContractMonthError):
        next_contract(bogus, narrow_nq)


def test_previous_contract_rejects_contract_month_not_supported_by_instrument():
    """Same InvalidContractMonthError guarantee for previous_contract()."""
    from app.futures.models import FuturesInstrument, SettlementType

    narrow_nq = FuturesInstrument(
        root_symbol="NQ",
        display_name="E-mini Nasdaq-100 (narrow test double)",
        exchange="CME",
        underlying="Nasdaq-100 Index",
        currency="USD",
        multiplier=Decimal("20"),
        tick_size=Decimal("0.25"),
        tick_value=Decimal("5.00"),
        settlement_type=SettlementType.CASH,
        contract_months=(ContractMonth.JUNE, ContractMonth.DECEMBER),
    )
    bogus = FuturesContract(root_symbol="NQ", year=2026, month=ContractMonth.MARCH)

    with pytest.raises(InvalidContractMonthError):
        previous_contract(bogus, narrow_nq)


# ============================================================================
# Phase 2.3 adversarial regression tests (third external audit findings)
# ============================================================================
#
# Every public function in this module must validate its
# instrument/contract parameters up front -- passing the wrong object
# type must never leak a raw AttributeError.


@pytest.mark.parametrize("bad_instrument", [None, "NQ", 123, object(), FuturesContract])
def test_quarter_contract_rejects_non_instrument(bad_instrument):
    with pytest.raises(InvalidInstrumentError):
        quarter_contract(bad_instrument, 2026, 9)


def test_next_contract_rejects_non_contract(nq):
    with pytest.raises(InvalidContractError):
        next_contract("bad", nq)


def test_next_contract_rejects_non_instrument(nq):
    valid = quarter_contract(nq, 2026, ContractMonth.SEPTEMBER)
    with pytest.raises(InvalidInstrumentError):
        next_contract(valid, "bad")


def test_previous_contract_rejects_non_contract(nq):
    with pytest.raises(InvalidContractError):
        previous_contract(12345, nq)


def test_previous_contract_rejects_non_instrument(nq):
    valid = quarter_contract(nq, 2026, ContractMonth.SEPTEMBER)
    with pytest.raises(InvalidInstrumentError):
        previous_contract(valid, None)


@pytest.mark.parametrize(
    "helper,args",
    [
        (points_to_ticks, (Decimal("1.00"),)),
        (ticks_to_points, (4,)),
        (points_to_dollars, (Decimal("1.00"),)),
        (ticks_to_dollars, (4,)),
    ],
)
def test_economics_helpers_reject_non_instrument(helper, args):
    with pytest.raises(InvalidInstrumentError):
        helper("bad-instrument", *args)


def test_economics_helpers_never_leak_attribute_error_for_wrong_instrument():
    for helper, args in (
        (points_to_ticks, (Decimal("1.00"),)),
        (ticks_to_points, (4,)),
        (points_to_dollars, (Decimal("1.00"),)),
        (ticks_to_dollars, (4,)),
    ):
        try:
            helper(None, *args)
        except InvalidInstrumentError:
            pass
        except AttributeError:
            pytest.fail(f"{helper.__name__} leaked a raw AttributeError for a bad instrument")
