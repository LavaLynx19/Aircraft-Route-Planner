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
    """Stands in for http.client.HTTPResponse: a readable body plus a status."""

    def __init__(self, status: int, body: bytes = b"") -> None:
        super().__init__(body)
        self.status = status


def _respond(monkeypatch, status: int, body: bytes = b"") -> None:
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda req, timeout=None: _Response(status, body)
    )


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
        )

    def test_departures_are_ok(self, monkeypatch):
        flight = {"number": "MU 5101"}
        _respond(monkeypatch, 200, json.dumps({"departures": [flight]}).encode())
        assert fetch_schedules.fetch_departures("test-key", "ZSPD", -720) == (
            "ok",
            [flight],
        )

    def test_a_200_with_no_departures_is_empty(self, monkeypatch):
        _respond(monkeypatch, 200, b'{"departures": []}')
        assert fetch_schedules.fetch_departures("test-key", "ZSPD", -720) == (
            "empty",
            [],
        )

    def test_an_unparseable_200_is_still_an_error_and_names_the_status(
        self, monkeypatch, capsys
    ):
        """Only the documented 204 means empty; a broken 200 body is a real fault."""
        _respond(monkeypatch, 200, b"")
        assert fetch_schedules.fetch_departures("test-key", "ZSPD", -720) == (
            "error",
            [],
        )
        assert "ZSPD: JSONDecodeError (HTTP 200)" in capsys.readouterr().out

    def test_a_network_failure_logs_no_status(self, monkeypatch, capsys):
        _record_urls(monkeypatch)
        assert fetch_schedules.fetch_departures("test-key", "ZSPD", -720) == (
            "error",
            [],
        )
        out = capsys.readouterr().out
        assert "ZSPD: URLError: " in out
        assert "HTTP" not in out
