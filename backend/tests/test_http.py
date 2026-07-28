"""Conditional-GET behaviour, driven entirely by real observed server quirks."""

from __future__ import annotations

import httpx
import pytest

from core.models import SourceState
from fetch.http import MAX_BODY_BYTES, fetch_feed, user_agent_for

BODY = b'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title></channel></rss>'
OTHER = b'<?xml version="1.0"?><rss version="2.0"><channel><title>y</title></channel></rss>'


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


class TestUserAgent:
    def test_default_is_honest_and_not_ai_branded(self) -> None:
        agent = user_agent_for("https://www.derstandard.at/rss")
        assert "MyNewsBot" in agent
        # derStandard, Spiegel, ORF, Zeit and FAZ Disallow these tokens by name
        # while leaving `User-agent: *` open.
        lowered = agent.lower()
        for blocked in ("claudebot", "gptbot", "anthropic", "ccbot", "perplexity"):
            assert blocked not in lowered

    def test_apple_developer_gets_safari(self) -> None:
        # developer.apple.com returns HTTP 500 to curl/Chrome/NetNewsWire UAs.
        assert "Safari" in user_agent_for("https://developer.apple.com/news/rss/news.rss")

    def test_override_matches_subdomains_not_suffix_collisions(self) -> None:
        assert "Safari" in user_agent_for("https://developer.apple.com/x")
        assert "Safari" not in user_agent_for("https://notdeveloper.apple.com.evil.test/x")


class TestConditionalGet:
    def test_304_reports_unchanged_without_a_body(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["If-Modified-Since"] == "Mon, 27 Jul 2026 10:00:00 GMT"
            assert request.headers["If-None-Match"] == 'W/"abc"'
            return httpx.Response(304)

        state = SourceState(id="s", etag='W/"abc"', last_modified="Mon, 27 Jul 2026 10:00:00 GMT")
        with _client(handler) as client:
            result = fetch_feed("https://example.com/feed", state, client=client)

        assert result.ok and result.unchanged
        assert result.body is None

    def test_gzip_is_requested(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            # httpx.Headers lower-cases keys, so normalize before comparing.
            seen.update({k.lower(): v for k, v in request.headers.items()})
            return httpx.Response(200, content=BODY)

        with _client(handler) as client:
            fetch_feed("https://example.com/feed", None, client=client)

        # ~80% bandwidth saving measured on derStandard, FAZ and heise.
        assert "gzip" in seen["accept-encoding"]

    def test_body_hash_catches_servers_that_ignore_validators(self) -> None:
        # About half the large feeds send neither ETag nor Last-Modified. Hashing
        # the body still lets us skip the expensive parse.
        def handler(request: httpx.Request) -> httpx.Response:
            assert "If-None-Match" not in request.headers
            return httpx.Response(200, content=BODY)

        with _client(handler) as client:
            first = fetch_feed("https://example.com/feed", None, client=client)
            state = SourceState(id="s", body_sha256=first.body_sha256)
            second = fetch_feed("https://example.com/feed", state, client=client)

        assert not first.unchanged
        assert second.unchanged
        assert second.body_sha256 == first.body_sha256

    def test_broken_etag_server_is_detected(self) -> None:
        # Azure blog, MS Research, .NET, devblogs, machinelearning.apple.com,
        # Daring Fireball and Al Jazeera all answer 200 + full body despite a
        # matching ETag. We learn this rather than hardcoding a blocklist.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=BODY, headers={"ETag": 'W/"same"'})

        state = SourceState(id="s", etag='W/"same"', body_sha256=None)
        with _client(handler) as client:
            first = fetch_feed("https://example.com/feed", state, client=client)
            state.body_sha256 = first.body_sha256
            second = fetch_feed("https://example.com/feed", state, client=client)

        assert second.unchanged
        assert second.etag_ignored is True

    def test_etag_not_offered_once_known_broken(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert "If-None-Match" not in request.headers
            return httpx.Response(200, content=OTHER)

        state = SourceState(id="s", etag='W/"x"', etag_works=False)
        with _client(handler) as client:
            result = fetch_feed("https://example.com/feed", state, client=client)

        assert result.ok and not result.unchanged

    def test_empty_last_modified_header_is_discarded(self) -> None:
        # developer.apple.com/news/releases sends Last-Modified with an empty
        # value; storing it would produce a meaningless request header next time.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=BODY, headers={"Last-Modified": "   "})

        with _client(handler) as client:
            result = fetch_feed("https://example.com/feed", None, client=client)

        assert result.last_modified is None

    def test_changed_body_is_reported_as_changed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=OTHER)

        state = SourceState(id="s", body_sha256="deadbeef")
        with _client(handler) as client:
            result = fetch_feed("https://example.com/feed", state, client=client)

        assert not result.unchanged
        assert result.body == OTHER


class TestFailureHandling:
    def test_http_500_is_an_error_not_a_crash(self) -> None:
        # developer.apple.com does exactly this for the wrong User-Agent.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        with _client(handler) as client:
            result = fetch_feed("https://example.com/feed", None, client=client)

        assert not result.ok
        assert result.status == 500

    def test_transport_error_is_captured(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        with _client(handler) as client:
            result = fetch_feed("https://example.com/feed", None, client=client)

        assert not result.ok
        assert "ConnectError" in (result.error or "")

    def test_oversized_body_is_truncated_not_fatal(self) -> None:
        # openai.com/news/rss.xml is 638 KB; one devblogs item was 566 KB alone.
        huge = b"<rss>" + b"x" * (MAX_BODY_BYTES + 1024)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=huge)

        with _client(handler) as client:
            result = fetch_feed("https://example.com/feed", None, client=client)

        assert result.ok
        assert result.body is not None
        assert len(result.body) <= MAX_BODY_BYTES

    def test_redirect_target_is_reported(self) -> None:
        # sixcolors, blog.google, heise/developer, openai and feeds.bloomberg all
        # redirect; persisting the resolved URL saves a hop on every later fetch.
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/old":
                return httpx.Response(301, headers={"Location": "https://example.com/new"})
            return httpx.Response(200, content=BODY)

        with _client(handler) as client:
            result = fetch_feed("https://example.com/old", None, client=client)

        assert result.resolved_url == "https://example.com/new"
