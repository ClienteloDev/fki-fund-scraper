from __future__ import annotations

from datetime import date

from fundscraper.sitemap_discovery import (
    candidate_sitemap_urls,
    filter_sitemap_entries,
    is_allowed,
    parse_sitemap,
    robots_disallowed_paths,
    robots_sitemap_urls,
    robots_url,
)

FUND_NAME = "Rezidento Alfa SICAV, a.s."

DOMAIN = "rezidentoalfa.cz"


URLSET = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://www.rezidentoalfa.cz/dokumenty/</loc>
    <lastmod>2026-03-01</lastmod>
  </url>
  <url>
    <loc>https://www.rezidentoalfa.cz/kariera/</loc>
  </url>
  <url>
    <loc>https://www.rezidentoalfa.cz/ke-stazeni/statut-fondu.pdf</loc>
    <lastmod>2025-11-20</lastmod>
  </url>
</urlset>
"""


SITEMAP_INDEX = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap>
    <loc>https://www.rezidentoalfa.cz/sitemap-pages.xml</loc>
  </sitemap>
  <sitemap>
    <loc>https://www.rezidentoalfa.cz/sitemap-documents.xml</loc>
  </sitemap>
</sitemapindex>
"""


def test_reads_a_plain_url_set() -> None:
    document = parse_sitemap(
        body=URLSET,
        sitemap_url="https://www.rezidentoalfa.cz/sitemap.xml",
    )

    assert not document.is_index

    assert not document.nested_sitemaps

    assert [entry.url for entry in document.entries] == [
        "https://www.rezidentoalfa.cz/dokumenty/",
        "https://www.rezidentoalfa.cz/kariera/",
        "https://www.rezidentoalfa.cz/ke-stazeni/statut-fondu.pdf",
    ]

    assert document.entries[0].last_modified == date(2026, 3, 1)

    assert document.entries[1].last_modified is None


def test_reads_a_sitemap_index() -> None:
    document = parse_sitemap(
        body=SITEMAP_INDEX,
        sitemap_url="https://www.rezidentoalfa.cz/sitemap.xml",
    )

    assert document.is_index

    assert not document.entries

    assert document.nested_sitemaps == (
        "https://www.rezidentoalfa.cz/sitemap-pages.xml",
        "https://www.rezidentoalfa.cz/sitemap-documents.xml",
    )


def test_recovers_the_locations_of_a_truncated_sitemap() -> None:
    truncated = (
        b"<urlset><url><loc>https://www.rezidentoalfa.cz/dokumenty/</loc></url>"
        b"<url><loc>https://www.rezidentoalfa.cz/pro-investory/</loc></ur"
    )

    document = parse_sitemap(
        body=truncated,
        sitemap_url="https://www.rezidentoalfa.cz/sitemap.xml",
    )

    assert [entry.url for entry in document.entries] == [
        "https://www.rezidentoalfa.cz/dokumenty/",
        "https://www.rezidentoalfa.cz/pro-investory/",
    ]


def test_resolves_a_relative_location_against_its_sitemap() -> None:
    document = parse_sitemap(
        body=b"<urlset><url><loc>/dokumenty/statut.pdf</loc></url></urlset>",
        sitemap_url="https://www.rezidentoalfa.cz/sitemap.xml",
    )

    assert document.entries[0].url == "https://www.rezidentoalfa.cz/dokumenty/statut.pdf"


def test_reads_the_sitemaps_named_by_robots() -> None:
    body = b"""
    User-agent: *
    Disallow: /admin/

    Sitemap: https://www.rezidentoalfa.cz/sitemap_index.xml
    sitemap: /sitemap-documents.xml
    """

    assert robots_sitemap_urls(
        body=body,
        base_url="https://www.rezidentoalfa.cz/robots.txt",
    ) == (
        "https://www.rezidentoalfa.cz/sitemap_index.xml",
        "https://www.rezidentoalfa.cz/sitemap-documents.xml",
    )


def test_reads_the_paths_robots_closes() -> None:
    body = b"""
User-agent: BadBot
Disallow: /

User-agent: *
Disallow: /admin/
Disallow: /wp-admin/
Allow: /wp-admin/admin-ajax.php
"""

    disallowed = robots_disallowed_paths(body=body)

    assert disallowed == (
        "/admin/",
        "/wp-admin/",
    )

    assert not is_allowed(
        url="https://www.rezidentoalfa.cz/admin/list",
        disallowed_paths=disallowed,
    )

    assert is_allowed(
        url="https://www.rezidentoalfa.cz/dokumenty/statut.pdf",
        disallowed_paths=disallowed,
    )


def test_keeps_only_the_relevant_sitemap_urls() -> None:
    document = parse_sitemap(
        body=URLSET,
        sitemap_url="https://www.rezidentoalfa.cz/sitemap.xml",
    )

    relevant = filter_sitemap_entries(
        entries=document.entries,
        fund_name=FUND_NAME,
        official_domain=DOMAIN,
    )

    urls = [item.url for item in relevant]

    assert "https://www.rezidentoalfa.cz/ke-stazeni/statut-fondu.pdf" in urls

    assert "https://www.rezidentoalfa.cz/dokumenty/" in urls

    assert "https://www.rezidentoalfa.cz/kariera/" not in urls


def test_drops_sitemap_urls_of_another_host() -> None:
    document = parse_sitemap(
        body=(
            b"<urlset><url><loc>https://www.jinyweb.cz/dokumenty/statut.pdf</loc></url></urlset>"
        ),
        sitemap_url="https://www.rezidentoalfa.cz/sitemap.xml",
    )

    assert not filter_sitemap_entries(
        entries=document.entries,
        fund_name=FUND_NAME,
        official_domain=DOMAIN,
    )


def test_offers_the_well_known_sitemap_addresses() -> None:
    assert candidate_sitemap_urls("https://www.rezidentoalfa.cz/pro-investory/") == (
        "https://www.rezidentoalfa.cz/sitemap.xml",
        "https://www.rezidentoalfa.cz/sitemap_index.xml",
        "https://www.rezidentoalfa.cz/sitemap-index.xml",
    )

    assert robots_url("https://www.rezidentoalfa.cz/a/b") == (
        "https://www.rezidentoalfa.cz/robots.txt"
    )

    assert candidate_sitemap_urls("not-a-url") == ()
