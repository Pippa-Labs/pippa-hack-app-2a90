"""What to Wear: live weather for a place, turned into an outfit.

Two requests to Open-Meteo (no key needed):

1. geocoding — place name -> coordinates
2. forecast  — current feels-like temperature and wind, plus the hourly
   chance of rain for the next 12 hours

Then a few fixed rules turn the weather into clothes, and, if the person
listed what they own, pick the items from their list that suit it.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

from pippa_agentapp_sdk import (
    AgentAppError,
    Call,
    Manifest,
    RateLimitedError,
    Result,
    ServiceUnavailableError,
    load_manifest_file,
    validate_manifest,
)

MANIFEST_PATH = Path(__file__).with_name("manifest.yaml")

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
USER_AGENT = "pippa-agent-app-what-to-wear/0.1 (https://github.com/Pippa-Labs)"
TIMEOUT_SECONDS = 5.0
DEFAULT_PLACE = "London"

RAIN_THRESHOLD = 40   # percent: at or above this, plan for rain
WIND_THRESHOLD = 30   # km/h: above this, plan for wind

# Keyword -> group. Checked in this order, so "raincoat" is waterproof before
# "coat" can make it warm, and "rain jacket" is waterproof before "jacket".
WARDROBE_GROUPS: list[tuple[str, tuple[str, ...]]] = [
    ("waterproof", ("raincoat", "waterproof", "umbrella", "rain", "cagoule")),
    ("footwear", ("boots", "trainers", "sneakers", "wellies")),
    ("warm", ("sweatshirt", "coat", "puffer", "parka", "jumper", "sweater", "hoodie",
              "fleece", "jacket", "cardigan", "jeans", "gloves", "scarf")),
    ("light", ("t-shirt", "tshirt", "tee", "shirt", "shorts", "sandals")),
]


class PlaceNotFound(AgentAppError):
    user_message = "I could not find that place."
    retryable = False


class App:
    def __init__(self) -> None:
        self._manifest = validate_manifest(load_manifest_file(MANIFEST_PATH))

    @property
    def manifest(self) -> Manifest:
        return self._manifest

    # ── The one network seam. Tests replace this on the instance. ──────────
    def _fetch(self, url: str, timeout: float = TIMEOUT_SECONDS) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()

    def handle(self, call: Call) -> Result:
        if call.outcome_id != "outfit.suggest":
            raise AgentAppError(f"this app cannot do {call.outcome_id!r}")

        place = (call.inputs.get("place") or "").strip() or DEFAULT_PLACE
        wardrobe = [str(item).strip() for item in (call.inputs.get("wardrobe") or [])
                    if str(item).strip()]

        name, country, lat, lon = self._locate(place)
        feels, rain, wind = self._weather(lat, lon)

        outfit = self._outfit(feels, rain, wind)
        picked = self._from_wardrobe(wardrobe, feels, rain)
        place_used = f"{name}, {country}" if country else name

        return Result(
            data={
                "place_used": place_used,
                "feels_like_c": feels,
                "rain_chance_percent": rain,
                "wind_kmh": wind,
                "outfit": outfit,
                "from_your_wardrobe": picked,
            },
            narrative=self._sentence(name, feels, rain, wind, picked),
        )

    # ── Network ─────────────────────────────────────────────────────────────
    def _get_json(self, url: str, what: str) -> dict:
        try:
            raw = self._fetch(url, timeout=TIMEOUT_SECONDS)
        except urllib.error.HTTPError as exc:          # must come before URLError
            if exc.code == 429:
                raise RateLimitedError(f"{what} rate limited")
            raise ServiceUnavailableError(f"{what} returned HTTP {exc.code}")
        except TimeoutError as exc:                    # not a URLError
            raise ServiceUnavailableError(f"{what} timed out: {exc}")
        except urllib.error.URLError as exc:
            raise ServiceUnavailableError(f"could not reach {what}: {exc.reason}")
        except OSError as exc:
            raise ServiceUnavailableError(f"{what} connection failed: {exc}")
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            raise ServiceUnavailableError(f"{what} sent something that is not JSON")
        if not isinstance(data, dict):
            raise ServiceUnavailableError(f"{what} sent an unexpected shape")
        return data

    def _locate(self, place: str) -> tuple[str, str, float, float]:
        url = GEOCODE_URL + "?" + urlencode(
            {"name": place, "count": 1, "language": "en", "format": "json"})
        data = self._get_json(url, "geocoding")
        results = data.get("results") or []   # unknown place: 200, no "results"
        if not results:
            error = PlaceNotFound(f"no geocoding result for {place!r}")
            error.user_message = f"I could not find anywhere called {place}."
            raise error
        top = results[0]
        try:
            return (str(top.get("name") or place), str(top.get("country") or ""),
                    float(top["latitude"]), float(top["longitude"]))
        except (KeyError, TypeError, ValueError):
            raise ServiceUnavailableError("geocoding result had no coordinates")

    def _weather(self, lat: float, lon: float) -> tuple[int, int, int]:
        url = FORECAST_URL + "?" + urlencode({
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,apparent_temperature,precipitation,wind_speed_10m",
            "hourly": "precipitation_probability",
            "forecast_hours": 12,
            "timezone": "auto",
        })
        data = self._get_json(url, "forecast")
        current = data.get("current") or {}
        feels = current.get("apparent_temperature")
        if feels is None:
            feels = current.get("temperature_2m")
        wind = current.get("wind_speed_10m")
        if feels is None or wind is None:
            raise ServiceUnavailableError("forecast had no current conditions")
        chances = [p for p in (data.get("hourly") or {}).get("precipitation_probability") or []
                   if isinstance(p, (int, float))]
        rain = max(chances) if chances else 0
        return round(feels), round(rain), round(wind)

    # ── Rules ───────────────────────────────────────────────────────────────
    @staticmethod
    def _outfit(feels: int, rain: int, wind: int) -> list[str]:
        if feels < 5:
            outfit = ["heavy coat", "jumper", "gloves"]
        elif feels < 12:
            outfit = ["coat", "jumper"]
        elif feels <= 18:
            outfit = ["light jacket"]
        elif feels <= 25:
            outfit = ["t-shirt"]
        else:
            outfit = ["t-shirt", "shorts"]

        windy = wind > WIND_THRESHOLD
        if rain >= RAIN_THRESHOLD:
            # An umbrella is little use in strong wind, so only offer it when calm.
            outfit.insert(1, "waterproof jacket" if windy else "waterproof jacket or umbrella")
            outfit.append("closed shoes")
        if windy:
            outfit.append("windproof layer")
        return outfit

    @staticmethod
    def _group(item: str) -> str | None:
        text = item.lower()
        for group, keywords in WARDROBE_GROUPS:
            if any(keyword in text for keyword in keywords):
                return group
        return None

    @classmethod
    def _from_wardrobe(cls, wardrobe: list[str], feels: int, rain: int) -> list[str]:
        wet = rain >= RAIN_THRESHOLD
        wanted = {"footwear"}
        wanted.add("warm" if feels <= 18 else "light")
        if wet:
            wanted.add("waterproof")

        picked: list[str] = []
        for item in wardrobe:
            group = cls._group(item)
            if group not in wanted:
                continue
            if wet and "sandal" in item.lower():
                continue   # rain calls for closed shoes
            if feels <= 25 and "shorts" in item.lower():
                continue   # shorts only once it is properly warm
            if item not in picked:
                picked.append(item)
        return picked

    @staticmethod
    def _sentence(name: str, feels: int, rain: int, wind: int, picked: list[str]) -> str:
        if feels < 5:
            wear = "a heavy coat, a jumper and gloves"
        elif feels < 12:
            wear = "a coat"
        elif feels <= 18:
            wear = "a light jacket"
        elif feels <= 25:
            wear = "a t-shirt"
        else:
            wear = "a t-shirt and shorts"

        windy = wind > WIND_THRESHOLD
        extra = ""
        if rain >= RAIN_THRESHOLD and windy:
            extra = (f" and a waterproof jacket, since an umbrella will struggle "
                     f"in {wind} km/h wind")
        elif rain >= RAIN_THRESHOLD:
            extra = " and take an umbrella"
        elif windy:
            extra = f" with a windproof layer for the {wind} km/h wind"

        sentence = (f"It feels like {feels}°C in {name} with a {rain}% chance of rain, "
                    f"so wear {wear}{extra}")
        if picked:
            sentence += f"; from your clothes, go for {', '.join(picked)}"
        return sentence + "."
