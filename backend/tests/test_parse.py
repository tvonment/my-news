"""Parsing and canonicalization.

The cases here are not invented — every URL shape and every mislabelled feed
format was observed on a live fetch on 2026-07-27.
"""

from __future__ import annotations

import pytest

from fetch.parse import (
    NotAFeedError,
    canonicalize_url,
    normalize_title,
    parse_feed,
    sniff_format,
    strip_html,
)


class TestCanonicalizeUrl:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            # derStandard appends ?ref=rss to every feed link.
            (
                "https://www.derstandard.at/story/300000123/titel?ref=rss",
                "https://derstandard.at/story/300000123/titel",
            ),
            # heise appends a long wt_mc campaign parameter.
            (
                "https://www.heise.de/news/x-9999.html?wt_mc=rss.red.ho.ho.atom.beitrag_plus.beitrag_plus",
                "https://heise.de/news/x-9999.html",
            ),
            # Spiegel uses a fragment rather than a query parameter.
            (
                "https://www.spiegel.de/panorama/artikel-a-123.html#ref=rss",
                "https://spiegel.de/panorama/artikel-a-123.html",
            ),
            # Generic ad-tech parameters.
            (
                "https://example.com/a?utm_source=x&utm_medium=y&fbclid=z&id=7",
                "https://example.com/a?id=7",
            ),
            # BBC's at_* pair.
            (
                "https://example.com/a?at_medium=RSS&at_campaign=KARANGA",
                "https://example.com/a",
            ),
        ],
    )
    def test_strips_tracking(self, raw: str, expected: str) -> None:
        assert canonicalize_url(raw) == expected

    def test_normalizes_host_and_scheme_case(self) -> None:
        assert canonicalize_url("HTTPS://WWW.Example.COM/Path/") == "https://example.com/Path"

    def test_preserves_meaningful_query(self) -> None:
        assert canonicalize_url("https://example.com/s?q=azure&page=2") == "https://example.com/s?q=azure&page=2"

    def test_is_idempotent(self) -> None:
        once = canonicalize_url("https://www.derstandard.at/story/1/t?ref=rss")
        assert canonicalize_url(once) == once

    def test_default_ports_are_dropped(self) -> None:
        assert canonicalize_url("https://example.com:443/a") == "https://example.com/a"
        assert canonicalize_url("http://example.com:80/a") == "http://example.com/a"

    def test_nondefault_port_is_kept(self) -> None:
        assert canonicalize_url("https://example.com:8443/a") == "https://example.com:8443/a"


class TestSniffFormat:
    def test_rss(self) -> None:
        assert sniff_format(b'<?xml version="1.0"?><rss version="2.0"><channel/></rss>') == "rss"

    def test_atom(self) -> None:
        assert sniff_format(b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"/>') == "atom"

    def test_rdf(self) -> None:
        # rss.orf.at serves RSS 1.0 / RDF.
        body = b'<?xml version="1.0"?><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"/>'
        assert sniff_format(body) == "rdf"

    def test_apple_newsroom_is_atom_despite_rss_filename(self) -> None:
        # apple.com/newsroom/rss-feed.rss serves Atom with Content-Type
        # application/rss+xml. Trusting the filename or header would break it.
        body = b'<?xml version="1.0" encoding="utf-8"?>\n<feed xmlns="http://www.w3.org/2005/Atom"><title>Apple</title></feed>'
        assert sniff_format(body) == "atom"

    def test_heise_rdf_filename_is_actually_rss2(self) -> None:
        # heise.de/rss/heise.rdf serves <rss version="2.0"> despite the extension.
        assert sniff_format(b'<rss version="2.0"><channel/></rss>') == "rss"

    def test_html_page_is_rejected(self) -> None:
        # azure.microsoft.com/en-us/updates/feed/ 302s to a 195 KB HTML page.
        with pytest.raises(NotAFeedError):
            sniff_format(b"<!DOCTYPE html>\n<html><head><title>Azure updates</title></head></html>")

    def test_garbage_is_rejected(self) -> None:
        with pytest.raises(NotAFeedError):
            sniff_format(b"not xml at all")


class TestNormalizeTitle:
    def test_strips_outlet_suffix(self) -> None:
        a = normalize_title("Nvidia kauft Startup fuer Milliarden - derStandard.at")
        b = normalize_title("Nvidia kauft Startup fuer Milliarden")
        assert a == b

    def test_case_and_whitespace_insensitive(self) -> None:
        assert normalize_title("  Hello   World  ") == normalize_title("hello world")

    def test_strips_markup(self) -> None:
        assert normalize_title("<b>Breaking</b> news here now") == normalize_title("Breaking news here now")


class TestStripHtml:
    def test_removes_tags_and_collapses_space(self) -> None:
        assert strip_html("<p>Hello   <b>world</b></p>") == "Hello world"

    def test_handles_none(self) -> None:
        assert strip_html(None) == ""


RSS_SAMPLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example</title>
    <item>
      <title>First story</title>
      <link>https://www.example.com/one?ref=rss</link>
      <description>&lt;p&gt;A lead paragraph.&lt;/p&gt;</description>
      <pubDate>Mon, 27 Jul 2026 10:00:00 +0000</pubDate>
    </item>
    <item>
      <title>Second story</title>
      <link>https://www.example.com/two</link>
      <description>Another lead.</description>
      <pubDate>Mon, 27 Jul 2026 09:00:00 +0000</pubDate>
    </item>
  </channel>
</rss>
"""


class TestParseFeed:
    def test_extracts_and_canonicalizes(self) -> None:
        parsed = parse_feed(RSS_SAMPLE)
        assert parsed.format == "rss"
        assert len(parsed.items) == 2
        assert parsed.items[0].canonical_url == "https://example.com/one"
        assert parsed.items[0].lead == "A lead paragraph."

    def test_newest_is_the_latest_item(self) -> None:
        parsed = parse_feed(RSS_SAMPLE)
        assert parsed.newest is not None
        assert parsed.newest.hour == 10

    def test_items_without_link_or_title_are_skipped(self) -> None:
        body = b'<rss version="2.0"><channel><item><description>orphan</description></item></channel></rss>'
        assert parse_feed(body).items == []

    def test_future_dates_are_clamped(self) -> None:
        from datetime import datetime, timezone

        body = b"""<rss version="2.0"><channel><item>
            <title>From the future</title><link>https://example.com/f</link>
            <pubDate>Mon, 27 Jul 2099 10:00:00 +0000</pubDate>
        </item></channel></rss>"""
        now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
        parsed = parse_feed(body, fetched_at=now)
        assert parsed.items[0].published == now
