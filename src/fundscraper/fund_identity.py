"""Which words of a fund's registered name identify that fund.

One answer, shared by the primary parser (`field_extraction`) and the grounded
review path (`grounding_packets`). Each used to keep a private copy of the noise
list, and the copies drifted - the SICAV legal form was noise in one and identity
in the other. Holding the list in one leaf is what stops that recurring.

Deliberately narrow. This is the *identity* vocabulary, not a general noise list,
and it must not absorb the ones in `discovery_priority`, `site_crawler`,
`field_definitions` or the `entity_key` family - see `docs/architecture.md`.
"""

from __future__ import annotations

import re
from typing import Final

from fundscraper.html_discovery import normalize_search_text

FUND_NAME_NOISE_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "a",
        "as",
        "s",
        "sicav",
        "fond",
        "fund",
        "fonds",
        "investicni",
        "investment",
        "spolecnost",
        "podfond",
        "subfund",
        "otevreny",
        "uzavreny",
        # "s proměnným základním kapitálem" is the legal form of a SICAV,
        # written out in the registered name of 29 of the canonical funds.
        # None of its three words tells one fund from another, and a mention
        # is only read as far as its "investiční fond" head, so a token
        # standing behind that head can never appear in one. Left in, it
        # made those funds unable to match their own legal name on the
        # primary path - on their own homepage the name then read as a
        # foreign fund and every value on the page was refused - while on
        # the grounded path it let a snippet from a document about any
        # other SICAV earn identity credit for this fund.
        "promennym",
        "zakladnim",
        "kapitalem",
    }
)


def fund_identity_tokens(
    fund_name: str,
) -> tuple[str, ...]:
    normalized = normalize_search_text(fund_name)

    raw_tokens = re.findall(
        r"[a-z0-9]+",
        normalized,
    )

    result: list[str] = []

    for token in raw_tokens:
        if token in FUND_NAME_NOISE_TOKENS:
            continue

        if len(token) < 2:
            continue

        if token not in result:
            result.append(token)

    return tuple(result)
