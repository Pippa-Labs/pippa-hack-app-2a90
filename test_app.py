"""Tests for What to Wear. The network is stubbed: `_fetch` is replaced."""

from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timezone

import pytest
from pippa_agentapp_sdk import LocalRunner, ServiceUnavailableError

from app import App, PlaceNotFound

NOW = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)

GEO_LONDON = {
    "results": [{"name": "London", "latitude": 51.50853, "longitude": -0.12574,
                 "country": "United Kingdom", "timezone": "Europe/London"}],
    "generationtime_ms": 0.12,
}
GEO_NOTHING = {"generationtime_ms": 0.05}   # what Open-Meteo really sends


def forecast(feels: float, wind: float, chances: list) -> dict:
    return {
        "timezone": "Europe/London",
        "current": {"time": "2026-11-20T15:00", "interval": 900,
                    "temperature_2m": feels + 2, "apparent_temperature": feels,
                    "precipitation": 0.0, "wind_speed_10m": wind},
        "hourly": {"time": [f"2026-11-20T{h:02d}:00" for h in range(len(chances))],
                   "precipitation_probability": chances},
    }


def make_app(geo: dict, weather: dict | None = None) -> App:
    app = App()
    seen: list[str] = []

    def fake_fetch(url, timeout=5.0):
        seen.append(url)
        if "geocoding-api" in url:
            return json.dumps(geo).encode()
        return json.dumps(weather).encode()

    app._fetch = fake_fetch
    app.seen_urls = seen
    return app


def run(app: App, inputs: dict, *, explicit: bool = False):
    return LocalRunner(app).call(
        "outfit.suggest", inputs, granted_secrets={}, now=NOW,
        request_id="t1", explicit=explicit,
    ).result


COLD_WET = forecast(feels=3.2, wind=12, chances=[20, 70, 55, None, 10])
WARM_DRY = forecast(feels=22.4, wind=8, chances=[0, 5, 10])


def test_the_manifest_is_valid():
    app = App()
    assert [o.id for o in app.manifest.outcomes] == ["outfit.suggest"]


def test_cold_and_wet_returns_a_coat_and_an_umbrella():
    result = run(make_app(GEO_LONDON, COLD_WET), {"place": "London"})
    data = result.data
    assert data["place_used"] == "London, United Kingdom"
    assert data["feels_like_c"] == 3
    assert data["rain_chance_percent"] == 70
    assert data["wind_kmh"] == 12
    assert data["outfit"][0] == "heavy coat"
    assert any("umbrella" in item for item in data["outfit"])
    assert "closed shoes" in data["outfit"]
    assert "umbrella" in result.narrative


def test_warm_and_dry_returns_a_t_shirt_and_no_umbrella():
    data = run(make_app(GEO_LONDON, WARM_DRY), {"place": "London"}).data
    assert data["outfit"] == ["t-shirt"]
    assert not any("umbrella" in item for item in data["outfit"])


def test_wardrobe_in_cold_rain_picks_puffer_and_raincoat_not_shorts():
    data = run(make_app(GEO_LONDON, COLD_WET),
               {"place": "London", "wardrobe": ["puffer", "raincoat", "shorts"]}).data
    assert data["from_your_wardrobe"] == ["puffer", "raincoat"]


def test_no_wardrobe_returns_an_empty_list():
    data = run(make_app(GEO_LONDON, COLD_WET), {"place": "London"}).data
    assert data["from_your_wardrobe"] == []


def test_strong_wind_says_umbrella_will_struggle():
    windy = forecast(feels=8, wind=45, chances=[80])
    result = run(make_app(GEO_LONDON, windy), {"place": "London"})
    assert "windproof layer" in result.data["outfit"]
    assert "waterproof jacket" in result.data["outfit"]
    assert "struggle" in result.narrative


def test_no_place_defaults_to_london():
    app = make_app(GEO_LONDON, WARM_DRY)
    run(app, {"place": ""})
    assert "name=London" in app.seen_urls[0]


def test_place_with_a_space_is_encoded():
    app = make_app(GEO_LONDON, WARM_DRY)
    run(app, {"place": "New York"})
    assert "name=New+York" in app.seen_urls[0]


def test_unknown_place_names_it():
    with pytest.raises(PlaceNotFound) as failure:
        run(make_app(GEO_NOTHING), {"place": "Narnia"})
    assert "Narnia" in failure.value.user_message


def test_unknown_place_is_refused_when_picked_by_name_too():
    with pytest.raises(PlaceNotFound):
        run(make_app(GEO_NOTHING), {"place": "Narnia"}, explicit=True)


@pytest.mark.parametrize("problem", [
    TimeoutError("timed out"),
    urllib.error.URLError("unreachable"),
])
def test_fetch_failing_returns_the_service_error(problem):
    app = App()

    def broken(url, timeout=5.0):
        raise problem

    app._fetch = broken
    with pytest.raises(ServiceUnavailableError):
        run(app, {"place": "London"})
