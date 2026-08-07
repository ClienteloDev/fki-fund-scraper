"""
Persistence of the extended fund data set.

The processing database keeps one row per observation, while the output
file keeps one field per fund. This module translates between the two in
both directions, so a run can be inspected, resumed and audited from the
database alone without re-reading the JSON.

Writing is additive: a value that could not be confirmed for the fund is
not written at all, which keeps the tables free of the observations the
extraction already refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from fundscraper.database import (
    AnnualReturnRecord,
    CapitalObservationRecord,
    DatabaseError,
    FundNewsRecord,
    FundPartyRecord,
    HistoricalValueRecord,
    list_annual_returns,
    list_capital_observations,
    list_fund_news,
    list_fund_parties,
    list_historical_values,
    record_annual_return,
    record_capital_observation,
    record_fund_news,
    record_historical_value,
    upsert_fund_party,
)
from fundscraper.extended_extraction import ExtendedFundFields
from fundscraper.output_models import (
    AnnualReturnObservation,
    AumMetricType,
    CapitalObservation,
    Confidence,
    FieldResult,
    FieldStatus,
    FundNewsItem,
    FundParty,
    HistoricalValueObservation,
    HistoricalValueSeries,
    HistoricalValueType,
    NewsSourceType,
    PartyRole,
    ReturnSeriesType,
    SeriesFrequency,
)


@dataclass(frozen=True, slots=True)
class ExtendedPersistenceSummary:
    """How many rows of each extended table one fund produced."""

    parties: int
    capital_observations: int
    annual_returns: int
    historical_values: int
    news_items: int

    @property
    def total(self) -> int:
        return (
            self.parties
            + self.capital_observations
            + self.annual_returns
            + self.historical_values
            + self.news_items
        )


def persist_extended_fields(
    *,
    database_path: Path,
    fund_id: str,
    extended: ExtendedFundFields,
    now: datetime | None = None,
) -> ExtendedPersistenceSummary:
    """Store every confirmed extended value of one fund."""

    parties = 0

    for result in (
        extended.manager,
        extended.administrator,
    ):
        if _store_party(
            database_path=database_path,
            fund_id=fund_id,
            result=result,
            now=now,
        ):
            parties += 1

    capital = 0

    if extended.aum_history.status is FieldStatus.FOUND and extended.aum_history.value is not None:
        for observation in extended.aum_history.value.observations:
            record_capital_observation(
                database_path,
                _capital_record(
                    fund_id=fund_id,
                    observation=observation,
                    result=extended.aum_history,
                ),
                now=now,
            )

            capital += 1

    returns = 0

    if (
        extended.annual_returns.status is FieldStatus.FOUND
        and extended.annual_returns.value is not None
    ):
        for annual in extended.annual_returns.value.observations:
            record_annual_return(
                database_path,
                _return_record(
                    fund_id=fund_id,
                    observation=annual,
                    result=extended.annual_returns,
                ),
                now=now,
            )

            returns += 1

    values = 0

    if (
        extended.historical_values.status is FieldStatus.FOUND
        and extended.historical_values.value is not None
    ):
        for series in extended.historical_values.value.series:
            for dated_value in series.observations:
                record_historical_value(
                    database_path,
                    _historical_record(
                        fund_id=fund_id,
                        series=series,
                        observation=dated_value,
                        result=extended.historical_values,
                    ),
                    now=now,
                )

                values += 1

    news = 0

    if extended.news.status is FieldStatus.FOUND and extended.news.value is not None:
        for item in extended.news.value.items:
            record_fund_news(
                database_path,
                _news_record(
                    fund_id=fund_id,
                    item=item,
                ),
                now=now,
            )

            news += 1

    return ExtendedPersistenceSummary(
        parties=parties,
        capital_observations=capital,
        annual_returns=returns,
        historical_values=values,
        news_items=news,
    )


def _store_party(
    *,
    database_path: Path,
    fund_id: str,
    result: FieldResult[FundParty],
    now: datetime | None,
) -> bool:
    if result.status is not FieldStatus.FOUND or result.value is None:
        return False

    upsert_fund_party(
        database_path,
        FundPartyRecord(
            fund_id=fund_id,
            role=result.value.role.value,
            name=result.value.name,
            legal_name=result.value.legal_name,
            ico=result.value.ico,
            web=str(result.value.web) if result.value.web is not None else None,
            source_url=_source_url(result),
            retrieved_at=_retrieved_at(result),
            confidence=_confidence(result),
            review_required=_review_required(result),
        ),
        now=now,
    )

    return True


def _capital_record[ValueT](
    *,
    fund_id: str,
    observation: CapitalObservation,
    result: FieldResult[ValueT],
) -> CapitalObservationRecord:
    return CapitalObservationRecord(
        fund_id=fund_id,
        metric_type=observation.metric_type.value,
        amount=observation.amount,
        currency=observation.currency,
        as_of=observation.as_of.isoformat(),
        share_class=observation.share_class,
        scope_type=(result.scope.type.value if result.scope is not None else None),
        source_url=_source_url(result),
        quote=_quote(result),
        page=_page(result),
        confidence=_confidence(result),
        review_required=_review_required(result),
    )


def _return_record[ValueT](
    *,
    fund_id: str,
    observation: AnnualReturnObservation,
    result: FieldResult[ValueT],
) -> AnnualReturnRecord:
    return AnnualReturnRecord(
        fund_id=fund_id,
        year=observation.year,
        series_type=observation.series_type.value,
        return_percent=observation.return_percent,
        share_class=observation.share_class,
        currency=observation.currency,
        source_url=_source_url(result),
        quote=_quote(result),
        page=_page(result),
        confidence=_confidence(result),
        review_required=_review_required(result),
    )


def _historical_record[ValueT](
    *,
    fund_id: str,
    series: HistoricalValueSeries,
    observation: HistoricalValueObservation,
    result: FieldResult[ValueT],
) -> HistoricalValueRecord:
    return HistoricalValueRecord(
        fund_id=fund_id,
        as_of=observation.as_of.isoformat(),
        value_type=series.value_type.value,
        value=observation.value,
        currency=observation.currency,
        share_class=series.share_class,
        unit=series.unit,
        frequency=series.frequency.value,
        source_url=_source_url(result),
        quote=_quote(result),
        page=_page(result),
        confidence=_confidence(result),
        review_required=_review_required(result),
    )


def _news_record(
    *,
    fund_id: str,
    item: FundNewsItem,
) -> FundNewsRecord:
    return FundNewsRecord(
        fund_id=fund_id,
        url=str(item.url),
        title=item.title,
        published_at=(item.published_at.isoformat() if item.published_at is not None else None),
        summary=item.summary,
        source_domain=item.source_domain,
        source_type=item.source_type.value,
        relation_confidence=item.relation_confidence.value,
    )


def _source_url[ValueT](
    result: FieldResult[ValueT],
) -> str | None:
    if result.source is None:
        return None

    return str(result.source.source.url)


def _retrieved_at[ValueT](
    result: FieldResult[ValueT],
) -> str | None:
    if result.source is None:
        return None

    return result.source.source.retrieved_at.isoformat()


def _quote[ValueT](
    result: FieldResult[ValueT],
) -> str | None:
    return result.source.quote if result.source is not None else None


def _page[ValueT](
    result: FieldResult[ValueT],
) -> int | None:
    return result.source.page if result.source is not None else None


def _confidence[ValueT](
    result: FieldResult[ValueT],
) -> str | None:
    return result.extraction.confidence.value if result.extraction is not None else None


def _review_required[ValueT](
    result: FieldResult[ValueT],
) -> bool:
    return result.extraction.review_required if result.extraction is not None else False


@dataclass(frozen=True, slots=True)
class StoredExtendedFields:
    """What the database holds for one fund, read back as output models."""

    parties: tuple[FundParty, ...]
    capital_observations: tuple[CapitalObservation, ...]
    annual_returns: tuple[AnnualReturnObservation, ...]
    historical_values: tuple[HistoricalValueSeries, ...]
    news: tuple[FundNewsItem, ...]


def load_extended_fields(
    *,
    database_path: Path,
    fund_id: str,
) -> StoredExtendedFields:
    """Read the stored extended values of one fund back into models."""

    try:
        party_rows = list_fund_parties(
            database_path,
            fund_id=fund_id,
        )

        capital_rows = list_capital_observations(
            database_path,
            fund_id=fund_id,
        )

        return_rows = list_annual_returns(
            database_path,
            fund_id=fund_id,
        )

        value_rows = list_historical_values(
            database_path,
            fund_id=fund_id,
        )

        news_rows = list_fund_news(
            database_path,
            fund_id=fund_id,
        )
    except DatabaseError:
        raise

    return StoredExtendedFields(
        parties=tuple(_party_of(row) for row in party_rows),
        capital_observations=tuple(_capital_of(row) for row in capital_rows),
        annual_returns=tuple(_return_of(row) for row in return_rows),
        historical_values=_series_of(value_rows),
        news=tuple(_news_of(row) for row in news_rows),
    )


def _party_of(
    row: FundPartyRecord,
) -> FundParty:
    return FundParty(
        role=PartyRole(row.role),
        name=row.name,
        legal_name=row.legal_name,
        ico=row.ico,
    )


def _capital_of(
    row: CapitalObservationRecord,
) -> CapitalObservation:
    return CapitalObservation(
        amount=row.amount,
        currency=row.currency,
        metric_type=AumMetricType(row.metric_type),
        as_of=date.fromisoformat(row.as_of),
        share_class=row.share_class or None,
    )


def _return_of(
    row: AnnualReturnRecord,
) -> AnnualReturnObservation:
    return AnnualReturnObservation(
        year=row.year,
        return_percent=row.return_percent,
        series_type=ReturnSeriesType(row.series_type),
        share_class=row.share_class or None,
        currency=row.currency or None,
    )


def _series_of(
    rows: list[HistoricalValueRecord],
) -> tuple[HistoricalValueSeries, ...]:
    """Group stored rows back into one series per measured quantity."""

    grouped: dict[
        tuple[str, str, str, str],
        list[HistoricalValueRecord],
    ] = {}

    for row in rows:
        key = (
            row.value_type,
            row.share_class or "",
            row.currency,
            row.frequency,
        )

        grouped.setdefault(
            key,
            [],
        ).append(row)

    series: list[HistoricalValueSeries] = []

    for (value_type, share_class, currency, frequency), items in sorted(grouped.items()):
        series.append(
            HistoricalValueSeries(
                value_type=HistoricalValueType(value_type),
                currency=currency,
                frequency=SeriesFrequency(frequency),
                share_class=share_class or None,
                unit=items[0].unit,
                observations=sorted(
                    (
                        HistoricalValueObservation(
                            as_of=date.fromisoformat(item.as_of),
                            value=item.value,
                            currency=item.currency,
                        )
                        for item in items
                    ),
                    key=lambda observation: observation.as_of,
                ),
            )
        )

    return tuple(series)


def _news_of(
    row: FundNewsRecord,
) -> FundNewsItem:
    return FundNewsItem(
        title=row.title,
        url=row.url,  # type: ignore[arg-type]
        source_domain=row.source_domain,
        source_type=NewsSourceType(row.source_type),
        published_at=(date.fromisoformat(row.published_at) if row.published_at else None),
        summary=row.summary,
        relation_confidence=Confidence(row.relation_confidence),
    )
