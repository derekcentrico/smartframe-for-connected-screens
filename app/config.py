"""
Centralised configuration: global ``os.environ.get`` calls live here.

Per-frame HA entity lookups are in ha.py (dynamic key names).
"""

import os


def _safe_int(env_key: str, default: int) -> int:
    raw = os.environ.get(env_key, "")
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


def _safe_float(env_key: str, default: float) -> float:
    raw = os.environ.get(env_key, "")
    try:
        return float(raw)
    except (ValueError, TypeError):
        return default


# ---------------------
# Core paths & timing
# ---------------------
CACHE_BASE = os.environ.get("CACHE_BASE", "/cache")
MEDIA_BASE = os.environ.get("MEDIA_BASE", "/media/smartframes")
SLIDE_INTERVAL = _safe_int("SLIDE_INTERVAL", 25)

# ---------------------
# Weather
# ---------------------
WEATHER_LAT = os.environ.get("WEATHER_LAT", None)
WEATHER_LON = os.environ.get("WEATHER_LON", None)
WEATHER_TZ = os.environ.get("WEATHER_TZ", "UTC")
WEATHER_OFFSET_C = _safe_float("WEATHER_OFFSET_C", 0.0)

# ---------------------
# Home Assistant
# ---------------------
HA_URL = os.environ.get("HA_URL")
HA_TOKEN = os.environ.get("HA_TOKEN")

# ---------------------
# Video handling
# ---------------------
MAX_VIDEO_MB = _safe_int("MAX_VIDEO_MB", 12)
ENABLE_VIDEOS = os.environ.get("ENABLE_VIDEOS", "1") == "1"

# ---------------------
# HEIC support
# ---------------------
ENABLE_HEIC = os.environ.get("ENABLE_HEIC", "1") == "1"

# ---------------------
# Upload
# ---------------------
UPLOAD_AUTH_ENABLED = os.environ.get("UPLOAD_AUTH_ENABLED", "0") == "1"
UPLOAD_USERNAME = os.environ.get("UPLOAD_USERNAME", "")
UPLOAD_PASSWORD = os.environ.get("UPLOAD_PASSWORD", "")
MAX_UPLOAD_MB = _safe_int("MAX_UPLOAD_MB", 200)
