"""
Selecting a subset of the canonical input by website.

A scoped run has to be able to say "only the funds this administrator
hosts" without anyone editing funds.json. The filter therefore matches on
substrings of the input `web`, and never on anything derived later in the
pipeline.
"""

from __future__ import annotations

from fundscraper.input_loader import select_funds_by_web
from fundscraper.models import FundInput

FUNDS = [
    FundInput(name="Alfa SICAV", web="https://www.avantfunds.cz/fondy/alfa/"),
    FundInput(name="Beta SICAV", web="https://www.AMISTA.cz"),
    FundInput(name="Gama SICAV", web="https://www.gamafond.cz"),
    FundInput(name="Delta SICAV", web=None),
    FundInput(name="Epsilon SICAV", web="https://amista.cz/epsilon.html"),
]


def test_one_pattern_selects_only_its_funds() -> None:
    selected = select_funds_by_web(FUNDS, ["avantfunds"])

    assert [fund.name for fund in selected] == ["Alfa SICAV"]


def test_repeated_patterns_are_combined_with_or() -> None:
    selected = select_funds_by_web(FUNDS, ["amista", "avant"])

    assert [fund.name for fund in selected] == ["Alfa SICAV", "Beta SICAV", "Epsilon SICAV"]


def test_matching_ignores_case_on_both_sides() -> None:
    """The dataset writes the host as AMISTA.cz; the caller types amista."""

    assert [fund.name for fund in select_funds_by_web(FUNDS, ["amista"])] == [
        "Beta SICAV",
        "Epsilon SICAV",
    ]
    assert [fund.name for fund in select_funds_by_web(FUNDS, ["AMISTA"])] == [
        "Beta SICAV",
        "Epsilon SICAV",
    ]


def test_a_fund_without_a_website_never_matches() -> None:
    """
    There is nothing to test against, and quietly including it would
    widen a run that was asked to be narrow.
    """

    selected = select_funds_by_web(FUNDS, ["delta"])

    assert selected == []
    assert all(fund.web is not None for fund in select_funds_by_web(FUNDS, ["cz"]))


def test_input_order_is_preserved() -> None:
    selected = select_funds_by_web(FUNDS, ["cz"])

    names = [fund.name for fund in selected]

    assert names == sorted(names, key=lambda n: [f.name for f in FUNDS].index(n))
    assert names[0] == "Alfa SICAV"


def test_no_pattern_means_every_fund() -> None:
    assert select_funds_by_web(FUNDS, []) == list(FUNDS)
    assert select_funds_by_web(FUNDS, [""]) == list(FUNDS)


def test_the_input_list_is_not_mutated() -> None:
    original = list(FUNDS)

    select_funds_by_web(FUNDS, ["amista"])

    assert original == FUNDS
