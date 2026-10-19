"""
Weather fetching and caching via Open-Meteo.
"""

import logging
import time

import requests

from .config import WEATHER_LAT, WEATHER_LON, WEATHER_OFFSET_C, WEATHER_TZ

logger = logging.getLogger(__name__)

_weather_cache: dict = {"timestamp": 0, "data": None}


def fetch_weather():
    """Fetch current weather from Open-Meteo and cache it (10 min)."""
    if not WEATHER_LAT or not WEATHER_LON:
        return None

    now = time.time()
    if _weather_cache["data"] and now - _weather_cache["timestamp"] < 600:
        return _weather_cache["data"]

    try:
        params = {
            "latitude": WEATHER_LAT,
            "longitude": WEATHER_LON,
            "current_weather": "true",
            "timezone": WEATHER_TZ,
        }
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast", params=params, timeout=5
        )
        r.raise_for_status()
        data = r.json().get("current_weather", {})
        temp_c = data.get("temperature")
        windspeed = data.get("windspeed")
        code = data.get("weathercode")

        if temp_c is not None:
            temp_c = temp_c + WEATHER_OFFSET_C

        icon = "\u2600"
        desc = "Clear"

        if code is None:
            pass
        elif code in (0,):
            icon, desc = "\u2600", "Clear"
        elif code in (1, 2, 3):
            icon, desc = "\u26c5", "Partly cloudy"
        elif code in (45, 48):
            icon, desc = "\U0001f32b", "Fog"
        elif code in (51, 53, 55, 61, 63, 65, 80, 81, 82):
            icon, desc = "\U0001f327", "Rain"
        elif code in (71, 73, 75, 77, 85, 86):
            icon, desc = "\u2744", "Snow"
        elif code in (95, 96, 99):
            icon, desc = "\u26c8", "Thunderstorm"

        temp_f = None
        if temp_c is not None:
            temp_f = round((temp_c * 9.0 / 5.0) + 32.0, 1)

        weather = {
            "icon": icon,
            "description": desc,
            "temp_c": temp_c,
            "temp_f": temp_f,
            "windspeed": windspeed,
        }
        _weather_cache["timestamp"] = now
        _weather_cache["data"] = weather
        return weather
    except Exception:
        logger.warning("Failed to fetch weather from Open-Meteo", exc_info=True)
        return _weather_cache["data"]
