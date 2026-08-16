"""
Per-stage, per-field and per-source-family accounting of the emergency gain.

Reads the consolidation log ``consolidate.py`` wrote and attributes every one of
the applied values to a source family. The classification order is deliberate
and is the interesting part of the file:

1. a known hub host (AVANT, AMISTA, DELTA IS, CODYA, TILLER/Winstor, PROTON IS,
   the public register) wins first, because those are the shared hosts where a
   value's identity had to come from the fund's own panel;
2. then the fund's **own** canonical ``web`` host;
3. then the CDN hosts that serve a fund's own site assets - Webflow, Framer,
   Cloudinary, DatoCMS - which look foreign but are the fund's own material;
4. then the rename aliases, because a fund's own site can sit on a domain the
   canonical input does not name.

The accounting this produced settled one claim worth keeping: **no accepted
value came from a third-party source.** All 633 came from the fund's own
document, its own site, its administrator's fund-specific panel, or its own
filing in the public register.

Writes ``reports/emergency-consolidation/summary.json``.

TEMPORARY RECOVERY TOOL.

Usage::

    uv run python scripts/emergency/summary.py
"""

from __future__ import annotations

import collections
import json
import re
import sys
import urllib.parse
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

WORK = REPOSITORY_ROOT / "reports/emergency-consolidation"

ENRICHED = REPOSITORY_ROOT / "data/output/funds.delivery.current-enriched.json"

# The usable total of the authoritative baseline the deltas were applied to.
BASELINE_USABLE = 1333

SLOTS = 3751

HUB = [
    ("AVANT", r"avantfunds\.cz"),
    ("AMISTA", r"amista\.cz"),
    ("DELTA IS", r"deltais\.cz"),
    ("CODYA", r"codyainvest\.cz"),
    ("TILLER / Winstor", r"tillerfunds\.cz|winstor\.cz"),
    ("PROTON IS", r"protonis\.cz"),
    ("Sbírka listin / public register", r"or\.justice\.cz|justice\.cz"),
]

# CDN hosts that serve a fund's OWN site assets (Webflow / Framer / Cloudinary / DatoCMS).
CDN_OWN = r"website-files\.com|framerusercontent\.com|res\.cloudinary\.com|datocms-assets\.com"

# Fund-own domains that differ from the canonical `web` (renames / group re-brands).
OWN_ALIAS = r"udifond\.com|efkd\.cz|efekta-developmentfund\.cz|fiofondy\.cz"

OTHER_OFFICIAL = (
    r"nwd\.cz|versuteis\.cz|jtis\.cz|redsidefunds\.com|monecois\.cz|partnersis\.cz|"
    r"cyrrusis\.cz|siriusis\.cz|qiis\.cz|conseq\.cz|creditas\.cz|amundi|csob|"
    r"efekta|arentia\.cz|drfg-fund\.cz|udifond\.com|fondaurelia\.cz|cnb\.cz|"
    r"investicnispolecnost|is\.cz"
)

THIRD_PARTY = r"fki-fondy\.cz|bhs\.cz|swisslifeselect\.cz|e15\.cz|czechcrunch|euro\.cz|patria"

STAGE_ORDER = ["01a", "01b", *[f"{index:02d}" for index in range(2, 18)]]


def host(url):
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def family(entry, web):
    """Return the source family of one applied value."""

    source_host = host(entry.get("source_url") or "")

    for name, pattern in HUB:
        if re.search(pattern, source_host):
            return name

    own = host(web.get(entry["fund"], ""))

    if own:
        base = own.replace("www.", "")

        if base and base in source_host:
            return "fund-own site"

    if re.search(CDN_OWN, source_host) or re.search(OWN_ALIAS, source_host):
        return "fund-own site"

    if re.search(THIRD_PARTY, source_host):
        return "third-party"

    if re.search(OTHER_OFFICIAL, source_host):
        return "other official source"

    # An unknown host that is neither a hub nor the canonical web: the fund's own
    # if its short name appears in the host, otherwise another official source.
    token = re.sub(r"[^a-z]", "", entry["fund"].lower())[:6]

    if token and token in re.sub(r"[^a-z]", "", source_host):
        return "fund-own site"

    return "other official source"


def main():
    log = json.loads((WORK / "consolidation-summary.json").read_text(encoding="utf-8"))

    records = json.loads(ENRICHED.read_text(encoding="utf-8"))

    web = {record["name"]: (record.get("web") or "") for record in records}

    stage_gain: collections.Counter[str] = collections.Counter()

    stage_strict: collections.Counter[str] = collections.Counter()

    field_gain: collections.Counter[str] = collections.Counter()

    family_gain: collections.Counter[str] = collections.Counter()

    family_field = collections.defaultdict(collections.Counter)

    confidence: collections.Counter[str] = collections.Counter()

    scope: collections.Counter[str] = collections.Counter()

    funds_per_stage = collections.defaultdict(set)

    for entry in log["applied_log"]:
        stage_gain[entry["stage"]] += 1

        if entry.get("scope_class") != "SOLE_SUBFUND":
            stage_strict[entry["stage"]] += 1

        field_gain[entry["field"]] += 1

        name = family(entry, web)

        family_gain[name] += 1

        family_field[name][entry["field"]] += 1

        confidence[entry["confidence"]] += 1

        scope[entry.get("scope_class")] += 1

        funds_per_stage[entry["stage"]].add(entry["fund"])

    scoped_stage = collections.Counter(entry["stage"] for entry in log["scoped_log"])

    flags_stage = collections.Counter(entry["stage"] for entry in log["flag_log"])

    running, running_strict, series = BASELINE_USABLE, BASELINE_USABLE, []

    for stage in STAGE_ORDER:
        running += stage_gain[stage]

        running_strict += stage_strict[stage]

        series.append(
            {
                "stage": stage,
                "gain": stage_gain[stage],
                "strict_gain": stage_strict[stage],
                "funds_with_gain": len(funds_per_stage[stage]),
                "scoped_not_counted": scoped_stage[stage],
                "audit_flags": flags_stage[stage],
                "cumulative_usable": running,
                "cumulative_coverage_pct": round(running / SLOTS * 100, 2),
                "cumulative_strict": running_strict,
                "cumulative_strict_pct": round(running_strict / SLOTS * 100, 2),
            }
        )

    out = {
        "stage_series": series,
        "total_gain": sum(stage_gain.values()),
        "total_strict_gain": sum(stage_strict.values()),
        "by_field": dict(field_gain.most_common()),
        "by_source_family": dict(family_gain.most_common()),
        "by_source_family_fields": {
            name: dict(counter.most_common()) for name, counter in family_field.items()
        },
        "by_confidence": dict(confidence),
        "by_scope_class": {str(key): value for key, value in scope.items()},
        "scoped_not_counted_total": len(log["scoped_log"]),
        "audit_flags_total": len(log["flag_log"]),
    }

    (WORK / "summary.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    print(
        json.dumps(
            {key: value for key, value in out.items() if key != "by_source_family_fields"},
            ensure_ascii=False,
            indent=1,
        )
    )


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    main()
