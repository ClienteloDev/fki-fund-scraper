from __future__ import annotations

import json
from pathlib import Path

from fundscraper.fallback_sources import (
    FallbackField,
    FallbackSourceType,
    load_approved_fallback_sources,
)


def test_loads_and_orders_enabled_sources(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "fallback_sources.json"

    config_path.write_text(
        json.dumps(
            [
                {
                    "name": "Secondary Source",
                    "base_url": "https://secondary.example.com",
                    "source_type": "secondary_database",
                    "enabled": True,
                    "priority": 200,
                    "supported_fields": ["minimum_investment"],
                    "allowed_domains": [],
                },
                {
                    "name": "Official Registry",
                    "base_url": "https://registry.example.com",
                    "source_type": "official_registry",
                    "enabled": True,
                    "priority": 50,
                    "supported_fields": ["assets_under_management"],
                    "allowed_domains": [],
                },
                {
                    "name": "Disabled",
                    "base_url": "https://disabled.example.com",
                    "source_type": "secondary_database",
                    "enabled": False,
                    "priority": 1,
                    "supported_fields": ["fees"],
                    "allowed_domains": [],
                },
            ]
        ),
        encoding="utf-8",
    )

    sources = load_approved_fallback_sources(config_path)

    assert len(sources) == 2

    assert sources[0].name == "Official Registry"

    assert sources[0].source_type is FallbackSourceType.OFFICIAL_REGISTRY

    assert sources[1].supported_fields == [FallbackField.MINIMUM_INVESTMENT]
