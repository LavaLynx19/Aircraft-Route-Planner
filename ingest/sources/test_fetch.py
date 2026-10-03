"""Request building and response handling for both fetchers.

URL encoding (2026-09-10 review L1): ICAO codes come from a trusted CSV and are
constrained to 4 alphanumerics, so this is defence in depth — a code containing
`&` or `?` must not be able to add query parameters, and for AeroDataBox, where
the code is a path segment, a `/` must not be able to change the endpoint.

`urlopen` is always stubbed, so no request leaves the machine.
"""

import io
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

import fetch_flights
import fetch_schedules


class _Token:
    def token(self) -> str:
        return "test-token"


def _record_urls(monkeypatch) -> list:
    urls = []

    def fake_urlopen(req, timeout=None):
        urls.append(req.full_url)
        raise urllib.error.URLError("stubbed: no network in tests")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return urls


class TestOpenSkyURL:
    def test_reserved_characters_are_percent_encoded(self, monkeypatch):
        urls = _record_urls(monkeypatch)
        fetch_flights.fetch_departures(_Token(), "A&B?", 100, 200)

        expected = (
            f"{fetch_flights.OSK_API_BASE}/flights/departure"
            "?airport=A%26B%3F&begin=100&end=200"
        )
        assert urls == [expected]

    def test_an_ordinary_code_is_unchanged(self, monkeypatch):
        urls = _record_urls(monkeypatch)
        fetch_flights.fetch_departures(_Token(), "EGLL", 100, 200)

        expected = (
            f"{fetch_flights.OSK_API_BASE}/flights/departure"
            "?airport=EGLL&begin=100&end=200"
        )
        assert urls == [expected]


class TestAeroDataBoxURL:
    def test_code_cannot_escape_its_path_segment(self, monkeypatch):
        urls = _record_urls(monkeypatch)
        fetch_schedules.fetch_departures("test-key", "A/B?x", -720)

        assert urls[0].startswith(
            f"{fetch_schedules.ADB_API_BASE}/flights/airports/icao/A%2FB%3Fx"
            "?offsetMinutes=-720&"
        )

    def test_an_ordinary_code_is_unchanged(self, monkeypatch):
        urls = _record_urls(monkeypatch)
        fetch_schedules.fetch_departures("test-key", "ZSPD", -720)

        assert urls[0].startswith(
            f"{fetch_schedules.ADB_API_BASE}/flights/airports/icao/ZSPD"
            "?offsetMinutes=-720&"
        )


class _Response(io.BytesIO):
    """Stands in for http.client.HTTPResponse: a body, a status and headers."""

    def __init__(self, status: int, body: bytes = b"", headers=None) -> None:
        super().__init__(body)
        self.status = status
        self.headers = headers or {}


def _respond(monkeypatch, status: int, body: bytes = b"", headers=None) -> None:
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda req, timeout=None: _Response(status, body, headers),
    )


def _respond_error(monkeypatch, code: int, body: bytes, headers=None) -> None:
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, code, "stubbed", headers or {}, io.BytesIO(body)
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


def _unit_headers(remaining: int, limit: int = 600, reset: int = 3600) -> dict:
    return {
        "X-RateLimit-API-Units-Remaining": str(remaining),
        "X-RateLimit-API-Units-Limit": str(limit),
        "X-RateLimit-API-Units-Reset": str(reset),
    }


class TestAeroDataBoxResponses:
    def test_204_no_content_is_an_empty_window(self, monkeypatch):
        """The documented reply for a window with no departures, not an error.

        Recorded as an error before 2026-09-17. An error never flips the half, so
        each such airport re-queried the same known-empty half on every visit and
        its other half was never sampled.
        """
        _respond(monkeypatch, 204)
        assert fetch_schedules.fetch_departures("test-key", "UZFA", -1440) == (
            "empty",
            [],
            None,
        )

    def test_departures_are_ok(self, monkeypatch):
        flight = {"number": "MU 5101"}
        _respond(monkeypatch, 200, json.dumps({"departures": [flight]}).encode())
        assert fetch_schedules.fetch_departures("test-key", "ZSPD", -720) == (
            "ok",
            [flight],
            None,
        )

    def test_a_200_with_no_departures_is_empty(self, monkeypatch):
        _respond(monkeypatch, 200, b'{"departures": []}')
        assert fetch_schedules.fetch_departures("test-key", "ZSPD", -720) == (
            "empty",
            [],
            None,
        )

    def test_an_unparseable_200_is_still_an_error_and_names_the_status(
        self, monkeypatch, capsys
    ):
        """Only the documented 204 means empty; a broken 200 body is a real fault."""
        _respond(monkeypatch, 200, b"")
        assert fetch_schedules.fetch_departures("test-key", "ZSPD", -720) == (
            "error",
            [],
            None,
        )
        assert "ZSPD: JSONDecodeError (HTTP 200)" in capsys.readouterr().out

    def test_a_network_failure_logs_no_status(self, monkeypatch, capsys):
        _record_urls(monkeypatch)
        assert fetch_schedules.fetch_departures("test-key", "ZSPD", -720) == (
            "error",
            [],
            None,
        )
        out = capsys.readouterr().out
        assert "ZSPD: URLError: " in out
        assert "HTTP" not in out


class TestAeroDataBoxQuota:
    """Unit accounting comes from the provider's headers (ARCHITECTURE.md §4.1.8)."""

    NOW = datetime(2026, 10, 3, 8, 0, 0, tzinfo=timezone.utc)

    def test_headers_become_an_absolute_reset_time(self):
        quota = fetch_schedules.parse_quota(_unit_headers(412, reset=7200), self.NOW)
        assert quota == {
            "remaining": 412,
            "limit": 600,
            "reset_at": "2026-10-03T10:00:00+00:00",
            "observed_at": "2026-10-03T08:00:00+00:00",
        }

    def test_an_exhausted_quota_reports_minus_one_and_is_clamped(self):
        quota = fetch_schedules.parse_quota(_unit_headers(-1), self.NOW)
        assert quota["remaining"] == 0

    def test_absent_headers_are_none(self):
        assert fetch_schedules.parse_quota({}, self.NOW) is None

    def test_a_partial_or_malformed_set_is_none(self):
        partial = _unit_headers(412)
        del partial["X-RateLimit-API-Units-Reset"]
        assert fetch_schedules.parse_quota(partial, self.NOW) is None

        malformed = _unit_headers(412)
        malformed["X-RateLimit-API-Units-Remaining"] = "many"
        assert fetch_schedules.parse_quota(malformed, self.NOW) is None

    def test_a_success_carries_its_quota(self, monkeypatch):
        _respond(monkeypatch, 204, headers=_unit_headers(410))
        status, flights, quota = fetch_schedules.fetch_departures(
            "test-key", "UZFA", -1440
        )
        assert (status, flights) == ("empty", [])
        assert (quota["remaining"], quota["limit"]) == (410, 600)

    def test_the_quota_429_carries_its_quota_and_logs_the_body(
        self, monkeypatch, capsys
    ):
        body = b'{"message":"You have exceeded the MONTHLY quota for API Units"}'
        _respond_error(monkeypatch, 429, body, _unit_headers(-1))
        status, flights, quota = fetch_schedules.fetch_departures(
            "test-key", "UBBN", -1440
        )
        assert (status, flights) == ("quota_exceeded", [])
        assert quota["remaining"] == 0
        assert "UBBN: HTTP 429: " in capsys.readouterr().out

    def test_a_throttle_429_has_no_quota(self, monkeypatch):
        """Observed 2026-10-03: the per-second 429 carries no unit headers."""
        body = b'{"message":"You have exceeded the rate limit per second"}'
        _respond_error(monkeypatch, 429, body)
        assert fetch_schedules.fetch_departures("test-key", "ZBAA", -1440) == (
            "throttled",
            [],
            None,
        )
