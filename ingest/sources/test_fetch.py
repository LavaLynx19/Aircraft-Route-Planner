"""Request-URL encoding for both fetchers (2026-09-10 review L1).

ICAO codes come from a trusted CSV and are constrained to 4 alphanumerics, so
this is defence in depth: a code containing `&` or `?` must not be able to add
query parameters, and for AeroDataBox — where the code is a path segment — a
`/` must not be able to change the endpoint.

`urlopen` is stubbed to record the URL and fail like a dropped connection, so
no request leaves the machine.
"""

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
