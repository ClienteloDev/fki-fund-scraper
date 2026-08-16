"""
Which funds a named validation batch covers, and why each one is in it.

The crawler-recovery experiments run over small, deliberately chosen sets of
canonical funds rather than the whole input. Keeping the sets here means the
crawl, the acquisition manifest, the loss funnel and the reports all agree on
what a batch is, and that an earlier batch stays reproducible after a later one
is added.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


DELIVERY_FIELDS = (
    "investment_horizon",
    "minimum_investment",
    "target_return",
    "fees",
    "assets_under_management",
    "manager",
    "administrator",
    "aum_history",
    "annual_returns",
    "historical_values",
    "news",
)


@dataclass(frozen=True, slots=True)
class BatchMember:
    """One fund of a batch, with the reason it was chosen."""

    name: str
    role: str
    rationale: str


@dataclass(frozen=True, slots=True)
class Batch:
    """A named set of canonical funds and where its run is stored."""

    name: str
    cache_directory: str
    members: tuple[BatchMember, ...]

    @property
    def fund_names(self) -> tuple[str, ...]:
        return tuple(member.name for member in self.members)

    @property
    def root(self) -> Path:
        return REPOSITORY_ROOT / self.cache_directory

    def role_of(self, fund_name: str) -> str:
        for member in self.members:
            if member.name == fund_name:
                return member.role

        return "unknown"


SAMPLE8 = Batch(
    name="sample8",
    cache_directory="cache/sample8-crawler-recovery",
    members=tuple(
        BatchMember(name=name, role="first_eight", rationale="first eight canonical funds")
        for name in (
            "3M FUND MSI SICAV a.s.",
            "CARE SICAV, a.s.",
            "DOMOPLAN - Projekty Brno SICAV, a.s.",
            "KOOR ESG SICAV a.s.",
            "Lázeňský fond SICAV a.s.",
            "MINT rezidenční fond SICAV, a.s.",
            "Natland Real Estate SICAV, a.s.",
            "Nemomax investiční fond s proměnným základním kapitálem, a.s.",
        )
    ),
)


# The batch recommended by reports/sample8-crawler-report.md. Each third of it
# answers a different question, and the last third is a control that has to
# stay still.
BATCH10 = Batch(
    name="batch10",
    cache_directory="cache/batch10-validation",
    members=(
        BatchMember(
            name="PRAGORENT investiční fond s proměnným základním kapitálem, a.s.",
            role="boilerplate_and_exclusive_host",
            rationale="carries 's proměnným základním kapitálem'; puif.cz is its alone",
        ),
        BatchMember(
            name="Outulný investiční fond s proměnným základním kapitálem, a.s.",
            role="boilerplate_and_exclusive_host",
            rationale="carries the phrase; ouif.cz is its alone",
        ),
        BatchMember(
            name="DEKINVEST, investiční fond s proměnným základním kapitálem, a.s.",
            role="boilerplate_and_exclusive_host",
            rationale="carries the phrase; dekinvest.cz is its alone",
        ),
        BatchMember(
            name="SPM GROUP investiční fond s proměnným základním kapitálem, a.s.",
            role="boilerplate_only",
            rationale=(
                "carries the phrase, but spmgroup.cz is shared with SPM FINANCE, "
                "so the site register cannot apply - isolates the identity-token fix"
            ),
        ),
        BatchMember(
            name="VALOUR investiční fond s proměnným základním kapitálem, a.s.",
            role="boilerplate_only",
            rationale=(
                "carries the phrase and sits on the avantfunds.cz hub - "
                "isolates the identity-token fix on a hub"
            ),
        ),
        BatchMember(
            name="ALT investiční fond SICAV a.s.",
            role="exclusive_host_only",
            rationale=(
                "akro.cz is a manager brand host that only this fund claims; "
                "0 of 11 delivered today - the hardest test of the site register"
            ),
        ),
        BatchMember(
            name="CRESTYL SICAV a.s.",
            role="exclusive_host_only",
            rationale=(
                "crestyl.cz is a large developer's group site claimed by one fund; "
                "tests whether project valuations leak into fund assets"
            ),
        ),
        BatchMember(
            name="MAVERICK Fund SICAV, a.s.",
            role="exclusive_host_only",
            rationale="versuteis.cz is a manager brand host claimed by one fund",
        ),
        BatchMember(
            name="Numero Fund SICAV, a.s.",
            role="hub_control",
            rationale=(
                "avantfunds.cz, excluded from the register by construction; "
                "0 of 11 delivered, so any movement would be visible"
            ),
        ),
        BatchMember(
            name="QH Letting SICAV a.s.",
            role="hub_control",
            rationale="amista.cz, excluded from the register by construction",
        ),
    ),
)


BATCHES = {batch.name: batch for batch in (SAMPLE8, BATCH10)}


def resolve(name: str) -> Batch:
    """Return a batch by name, or fail with the names that do exist."""

    try:
        return BATCHES[name]
    except KeyError:
        known = ", ".join(sorted(BATCHES))

        raise SystemExit(f"unknown batch: {name}. Known batches: {known}") from None
