"""
Main Flask routes: frame display, media serving, motion, HA status, health, weather.
"""

import os
import re

from flask import Blueprint, abort, jsonify, render_template, send_from_directory
from werkzeug.security import safe_join

from .config import CACHE_BASE, MEDIA_BASE, SLIDE_INTERVAL
from .ha import fetch_ha_status, fetch_motion_state
from .media import get_cached_image, list_media
from .weather import fetch_weather

routes_bp = Blueprint("routes", __name__)

# Only allow alphanumeric, underscore, and hyphen in frame names.
_SAFE_FRAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _validated_frame_dir(frame_name: str) -> str:
    """Return the real path for a frame directory, or abort with 400/404."""
    if not _SAFE_FRAME_RE.match(frame_name):
        abort(400)
    folder = os.path.join(MEDIA_BASE, frame_name.lower())
    real_folder = os.path.realpath(folder)
    if not real_folder.startswith(os.path.realpath(MEDIA_BASE)):
        abort(403)
    return real_folder


@routes_bp.route("/frame/<frame_name>")
def frame(frame_name):
    if not _SAFE_FRAME_RE.match(frame_name):
        abort(400)

    files = list_media(frame_name)
    weather = fetch_weather()
    status_items = fetch_ha_status(frame_name)

    return render_template(
        "frame.html",
        frame_name=frame_name,
        files=files,
        slide_interval=SLIDE_INTERVAL,
        weather=weather,
        status_items=status_items,
    )


@routes_bp.route("/media/<frame_name>/<path:filename>")
def media(frame_name, filename):
    real_folder = _validated_frame_dir(frame_name)
    if not os.path.isdir(real_folder):
        abort(404)
    return send_from_directory(real_folder, filename)


@routes_bp.route("/img/<frame_name>/<path:filename>")
def img(frame_name, filename):
    real_folder = _validated_frame_dir(frame_name)
    if not os.path.isdir(real_folder):
        abort(404)

    src_full = safe_join(real_folder, filename)
    if src_full is None or not os.path.isfile(src_full):
        abort(404)

    rel_path = os.path.relpath(src_full, real_folder)
    cache_full = get_cached_image(frame_name, src_full, rel_path)

    if cache_full is None:
        abort(404)

    rel_cache = os.path.relpath(cache_full, CACHE_BASE)
    cache_dir = os.path.join(CACHE_BASE, os.path.dirname(rel_cache))
    cache_file = os.path.basename(cache_full)
    return send_from_directory(cache_dir, cache_file)


@routes_bp.route("/motion/<frame_name>")
def motion(frame_name):
    if not _SAFE_FRAME_RE.match(frame_name):
        abort(400)
    return jsonify(fetch_motion_state(frame_name))


@routes_bp.route("/ha_status/<frame_name>")
def ha_status(frame_name):
    if not _SAFE_FRAME_RE.match(frame_name):
        abort(400)
    return jsonify(fetch_ha_status(frame_name))


@routes_bp.route("/weather")
def weather_api():
    return jsonify(fetch_weather() or {})


@routes_bp.route("/media_list/<frame_name>")
def media_list(frame_name):
    """Return the current media URL list as JSON (used for soft recovery)."""
    if not _SAFE_FRAME_RE.match(frame_name):
        abort(400)
    return jsonify(list_media(frame_name))


@routes_bp.route("/health")
def health():
    return "ok", 200


@routes_bp.route("/")
def root():
    return "SmartFrame slideshow is running."
