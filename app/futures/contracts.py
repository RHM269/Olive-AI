"""Contract lifecycle and tick/price-economics helpers.

Thin, generic helpers built on top of :class:`~app.futures.models.FuturesInstrument`
and :class:`~app.futures.models.FuturesContract`. These operate the same
way for any instrument in the registry (NQ, MNQ, or any future
quarterly instrument) rather than duplicating per-symbol logic.

Phase 2.1 hardening note: ``next_contract``/``previous_contract`` must
never silently reinterpret a contract under a different instrument
(e.g. advancing an NQ contract "as if" it belonged to MNQ) -- both
verify ``contract.root_symbol == instrument.root_symbol`` first and
raise :class:`~app.futures.models.ContractInstrumentMismatchError`
otherwise. They also raise
:class:`~app.futures.models.InvalidContractMonthError` (never a bare
``ValueError`` from a missed ``list.index()`` lookup) if the
contract's month is not actually one of the instrument's supported
quarterly months.

Phase 2.3 hardening note: a third external audit found that every
public function in this module -- ``quarter_contract``,
``next_contract``, ``previous_contract``, and the tick/price/dollar
economics wrappers -- took its ``instrument``/``contract`` parameters
on faith: passing the wrong object type (e.g. a plain string) leaked a
raw ``AttributeError`` the first time the function touched one of the
object's attributes. Every function below now validates its domain-
object parameters up front with the shared
``app.futures.models._require_instrument`` / ``_require_contract``
validators, raising :class:`~app.futures.models.InvalidInstrumentError`
/ :class:`~app.futures.models.InvalidContractError` instead.
"""

from __future__ import annotations

from decimal import Decimal

from app.futures.models import (
    ContractInstrumentMismatchError,
    ContractMonth,
    FuturesContract,
    FuturesInstrument,
    InvalidContractMonthError,
    _require_contract,
    _require_instrument,
)

# -- Contract lifecycle (quarter-to-quarter navigation) -------------------------


def quarter_contract(
    instrument: FuturesInstrument, year: int, month: ContractMonth | int
) -> FuturesContract:
    """Build a quarterly :class:`FuturesContract` for ``instrument``.

    Delegates to ``instrument.quarter_contract``, which normalizes
    ``month`` and raises :class:`InvalidContractMonthError` for a
    non-quarterly or unsupported value rather than letting
    ``ContractMonth(month)`` leak a bare ``ValueError``.

    Raises :class:`~app.futures.models.InvalidInstrumentError` if
    ``instrument`` is not a :class:`FuturesInstrument`.
    """
    instrument = _require_instrument(instrument, context="quarter_contract")
    return instrument.quarter_contract(year, month)


def _require_same_instrument(contract: FuturesContract, instrument: FuturesInstrument) -> None:
    """Guard against silently navigating a contract under the wrong instrument.

    Both ``contract.root_symbol`` and ``instrument.root_symbol`` are
    guaranteed canonical (stripped, upper-cased) by their own
    ``__post_init__`` validation, so a direct equality check is safe
    and sufficient here.
    """
    if contract.root_symbol != instrument.root_symbol:
        raise ContractInstrumentMismatchError(
            f"Cannot navigate {contract.display_code} (root '{contract.root_symbol}') using "
            f"instrument '{instrument.root_symbol}' -- next_contract()/previous_contract() never "
            "silently reinterpret a contract under a different instrument."
        )


def _month_index(contract: FuturesContract, instrument: FuturesInstrument, months_sorted: list[int]) -> int:
    current = int(contract.month)
    if current not in months_sorted:
        raise InvalidContractMonthError(
            f"{contract.display_code}'s month ({contract.month.name}) is not a supported contract "
            f"month for {instrument.root_symbol} (supported: {months_sorted})"
        )
    return months_sorted.index(current)


def next_contract(contract: FuturesContract, instrument: FuturesInstrument) -> FuturesContract:
    """Return the next quarterly contract after ``contract`` for ``instrument``.

    Handles year rollover generically (e.g. NQZ6 -> NQH7), driven by
    ``instrument.contract_months`` rather than a hard-coded quarterly
    assumption.

    Raises :class:`ContractInstrumentMismatchError` if ``contract``
    does not belong to ``instrument``, and
    :class:`InvalidContractMonthError` if ``contract``'s month is not
    one ``instrument`` actually supports. Raises
    :class:`~app.futures.models.InvalidContractError` /
    :class:`~app.futures.models.InvalidInstrumentError` if either
    parameter is not the right object type.
    """
    contract = _require_contract(contract, context="next_contract")
    instrument = _require_instrument(instrument, context="next_contract")
    _require_same_instrument(contract, instrument)
    months_sorted = sorted(int(m) for m in instrument.contract_months)
    idx = _month_index(contract, instrument, months_sorted)
    if idx == len(months_sorted) - 1:
        return instrument.quarter_contract(contract.year + 1, ContractMonth(months_sorted[0]))
    return instrument.quarter_contract(contract.year, ContractMonth(months_sorted[idx + 1]))


def previous_contract(contract: FuturesContract, instrument: FuturesInstrument) -> FuturesContract:
    """Return the quarterly contract immediately before ``contract``.

    Handles year rollback generically (e.g. NQH7 -> NQZ6).

    Raises :class:`ContractInstrumentMismatchError` if ``contract``
    does not belong to ``instrument``, and
    :class:`InvalidContractMonthError` if ``contract``'s month is not
    one ``instrument`` actually supports. Raises
    :class:`~app.futures.models.InvalidContractError` /
    :class:`~app.futures.models.InvalidInstrumentError` if either
    parameter is not the right object type.
    """
    contract = _require_contract(contract, context="previous_contract")
    instrument = _require_instrument(instrument, context="previous_contract")
    _require_same_instrument(contract, instrument)
    months_sorted = sorted(int(m) for m in instrument.contract_months)
    idx = _month_index(contract, instrument, months_sorted)
    if idx == 0:
        return instrument.quarter_contract(contract.year - 1, ContractMonth(months_sorted[-1]))
    return instrument.quarter_contract(contract.year, ContractMonth(months_sorted[idx - 1]))


# -- Tick / price / dollar economics --------------------------------------------
#
# Thin delegating wrappers around FuturesInstrument's economics methods, kept
# here so callers have one obvious place (app.futures.contracts) to import
# contract-economics helpers from. Validation of the point/tick VALUES
# (e.g. rejecting fractional or bool tick counts, non-finite Decimals) lives
# once on FuturesInstrument and is inherited for free; these wrappers add
# only the one check FuturesInstrument's own methods can't perform
# themselves -- that ``instrument`` is actually a FuturesInstrument.


def points_to_ticks(instrument: FuturesInstrument, points: Decimal) -> int:
    instrument = _require_instrument(instrument, context="points_to_ticks")
    return instrument.points_to_ticks(points)


def ticks_to_points(instrument: FuturesInstrument, ticks: int) -> Decimal:
    instrument = _require_instrument(instrument, context="ticks_to_points")
    return instrument.ticks_to_points(ticks)


def points_to_dollars(instrument: FuturesInstrument, points: Decimal) -> Decimal:
    instrument = _require_instrument(instrument, context="points_to_dollars")
    return instrument.points_to_dollars(points)


def ticks_to_dollars(instrument: FuturesInstrument, ticks: int) -> Decimal:
    instrument = _require_instrument(instrument, context="ticks_to_dollars")
    return instrument.ticks_to_dollars(ticks)
