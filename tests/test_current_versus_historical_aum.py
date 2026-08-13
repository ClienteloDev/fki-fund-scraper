"""
Current assets against the fund's own history.

``assets_under_management`` is what the fund holds now; ``aum_history``
is every dated observation it published. The two are read from the same
documents and are easy to confuse, and one delivered output confused
them: a 2023 net asset value stood as the current figure while the
fund's own history already held a 2025 one.

The rule here refuses a current figure for being superseded, never for
being old, and it never removes anything from the history.
"""

from __future__ import annotations

from datetime import date

from fundscraper.extended_validation import (
    FundRecordView,
    ValidationCode,
    validate_cross_fields,
)
from fundscraper.output_models import (
    AssetsUnderManagementValue,
    AumMetricType,
    CapitalObservation,
)


def observation(
    amount: float,
    year: int,
    *,
    metric: AumMetricType = AumMetricType.NAV,
) -> CapitalObservation:
    return CapitalObservation(
        amount=amount,
        currency="CZK",
        metric_type=metric,
        as_of=date(year, 12, 31),
    )


def current(
    amount: float,
    year: int,
    *,
    metric: AumMetricType = AumMetricType.NAV,
) -> AssetsUnderManagementValue:
    return AssetsUnderManagementValue(
        amount=amount,
        currency="CZK",
        metric_type=metric,
        as_of=date(year, 12, 31),
    )


def codes(findings: object) -> set[str]:
    return {finding.code.value for finding in findings}  # type: ignore[attr-defined]


def test_a_superseded_current_figure_is_reported() -> None:
    """
    MAM Private Equity Fund: 125.7 million CZK for 2023 delivered as current.

    The fund's own history held 328 million for 2025, read from its own
    newsletter. Both are true; only one is current.
    """

    findings = validate_cross_fields(
        FundRecordView(
            fund_name="MAM Private Equity Fund SICAV a.s.",
            assets_under_management=current(125_700_000.0, 2023),
            aum_observations=(
                observation(125_700_000.0, 2023),
                observation(328_000_000.0, 2025),
            ),
        )
    )

    assert ValidationCode.NEWER_AUM_OBSERVATION_EXISTS.value in codes(findings)

    detail = next(
        finding.detail
        for finding in findings
        if finding.code is ValidationCode.NEWER_AUM_OBSERVATION_EXISTS
    )

    assert "2025-12-31" in detail


def test_the_newest_observation_delivered_as_current_keeps_passing() -> None:
    """The other side: nothing supersedes the newest figure."""

    assert ValidationCode.NEWER_AUM_OBSERVATION_EXISTS.value not in codes(
        validate_cross_fields(
            FundRecordView(
                fund_name="MAM Private Equity Fund SICAV a.s.",
                assets_under_management=current(328_000_000.0, 2025),
                aum_observations=(
                    observation(125_700_000.0, 2023),
                    observation(328_000_000.0, 2025),
                ),
            )
        )
    )


def test_an_old_current_figure_with_no_newer_observation_is_not_reported() -> None:
    """
    Age alone is never the complaint.

    A fund that published one figure in 2019 and nothing since has an old
    current value and no better one. Refusing it here would be a blind
    rule about dates rather than a comparison of evidence, and the
    existing five-year staleness rule already speaks for that case.
    """

    assert ValidationCode.NEWER_AUM_OBSERVATION_EXISTS.value not in codes(
        validate_cross_fields(
            FundRecordView(
                fund_name="Example fond SICAV, a.s.",
                assets_under_management=current(530_000_000.0, 2019),
                aum_observations=(observation(530_000_000.0, 2019),),
            )
        )
    )


def test_a_newer_manager_level_figure_does_not_supersede_the_fund() -> None:
    """The assets of the manager are not a newer reading of the fund."""

    assert ValidationCode.NEWER_AUM_OBSERVATION_EXISTS.value not in codes(
        validate_cross_fields(
            FundRecordView(
                fund_name="Example fond SICAV, a.s.",
                assets_under_management=current(530_000_000.0, 2023),
                aum_observations=(
                    observation(530_000_000.0, 2023),
                    observation(
                        42_000_000_000.0,
                        2025,
                        metric=AumMetricType.MANAGER_AUM,
                    ),
                ),
            )
        )
    )


def test_the_history_is_never_asked_to_give_up_an_old_observation() -> None:
    """
    The contract the fix must not break.

    Reporting a superseded current figure says nothing about the history
    it was compared against: both observations are still there, and the
    rule produces exactly one finding, about the current field.
    """

    view = FundRecordView(
        fund_name="MAM Private Equity Fund SICAV a.s.",
        assets_under_management=current(125_700_000.0, 2023),
        aum_observations=(
            observation(125_700_000.0, 2023),
            observation(328_000_000.0, 2025),
        ),
    )

    findings = validate_cross_fields(view)

    assert len(view.aum_observations) == 2

    superseded = [
        finding
        for finding in findings
        if finding.code is ValidationCode.NEWER_AUM_OBSERVATION_EXISTS
    ]

    assert len(superseded) == 1
