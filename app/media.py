"""
Media listing, image caching / resizing, and HEIC support.
"""

import logging
import os
import random
import time

from PIL import Image, ImageOps

from .config import CACHE_BASE, MEDIA_BASE, MAX_VIDEO_MB, ENABLE_VIDEOS, ENABLE_HEIC

logger = logging.getLogger(__name__)

# ---------------------
# HEIC support
# ---------------------
HEIC_SUPPORTED = False
if ENABLE_HEIC:
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
        HEIC_SUPPORTED = True
    except ImportError:
        HEIC_SUPPORTED = False
        logger.warning("pillow-heif not installed; HEIC files will be skipped")

# ---------------------
# Ensure cache dir exists
# ---------------------
if not os.path.isdir(CACHE_BASE):
    try:
        os.makedirs(CACHE_BASE, exist_ok=True)
    except OSError:
        logger.warning("Could not create cache directory %s", CACHE_BASE)

# ---------------------
# Image caching / resizing
# ---------------------


def _safe_cache_path(frame_name: str, rel_path: str) -> str:
    """Return full path to cached image for a given frame and relative source path."""
    cache_dir = os.path.join(
        CACHE_BASE, frame_name.lower(), os.path.dirname(rel_path)
    )
    if not os.path.isdir(cache_dir):
        try:
            os.makedirs(cache_dir, exist_ok=True)
        except OSError:
            logger.warning("Could not create cache subdirectory %s", cache_dir)

    base, _ext = os.path.splitext(os.path.basename(rel_path))
    cache_filename = base + "_1080x1920.jpg"
    return os.path.join(cache_dir, cache_filename)


def get_cached_image(frame_name: str, full_src_path: str, rel_path: str):
    """Return absolute path to a resized 1080x1920 image, creating it if needed.

    Returns ``None`` if the image cannot be processed.
    """
    cache_path = _safe_cache_path(frame_name, rel_path)

    if os.path.isfile(cache_path):
        # Invalidate if source is newer than cache
        try:
            if os.path.getmtime(full_src_path) <= os.path.getmtime(cache_path):
                return cache_path
        except OSError:
            return None
        # else fall through to regenerate

    try:
        im = Image.open(full_src_path)
        im = ImageOps.exif_transpose(im)
        im = im.convert("RGB")
        im.thumbnail((1080, 1920), Image.LANCZOS)
        im.save(cache_path, "JPEG", quality=72, optimize=True, progressive=True)
        return cache_path
    except Exception:
        logger.warning("Failed to cache image %s", full_src_path, exc_info=True)
        return None


# ---------------------
# Media list caching with mtime invalidation
# ---------------------
_media_cache: dict = {}  # frame_name -> {"files": [...], "mtime": float, "scanned_at": float}


def invalidate_media_cache(frame_name: str):
    """Remove cached media list for a frame so the next call rescans."""
    _media_cache.pop(frame_name.lower(), None)


def _folder_max_mtime(folder: str) -> float:
    """Return the newest mtime of any file under *folder*."""
    max_mt = 0.0
    for root, _dirs, filenames in os.walk(folder):
        for name in filenames:
            try:
                mt = os.path.getmtime(os.path.join(root, name))
                if mt > max_mt:
                    max_mt = mt
            except OSError:
                pass
    return max_mt


def list_media(frame_name: str):
    """Return a shuffled list of media URLs for the given frame."""
    key = frame_name.lower()
    folder = os.path.join(MEDIA_BASE, key)
    if not os.path.isdir(folder):
        return []

    max_mt = _folder_max_mtime(folder)

    cached = _media_cache.get(key)
    cache_age = time.time() - cached.get("scanned_at", 0) if cached else float("inf")
    if cached and cached["mtime"] >= max_mt and cache_age < 300:
        # Return a fresh shuffle of the cached list
        result = cached["files"][:]
        random.shuffle(result)
        return result

    image_exts = [".jpg", ".jpeg", ".png"]
    if HEIC_SUPPORTED:
        image_exts.extend([".heic", ".heif"])
    image_exts = tuple(image_exts)
    video_exts = (".mp4",)
    max_video_bytes = MAX_VIDEO_MB * 1024 * 1024

    files = []

    for root, _dirs, filenames in os.walk(folder):
        for name in filenames:
            lower = name.lower()
            full_path = os.path.join(root, name)
            rel_path = os.path.relpath(full_path, folder)
            rel_url_path = rel_path.replace("\\", "/")

            if lower.endswith(image_exts):
                url = "/img/%s/%s" % (key, rel_url_path)
                files.append(url)
                continue

            if ENABLE_VIDEOS and lower.endswith(video_exts):
                try:
                    size = os.path.getsize(full_path)
                except OSError:
                    continue
                if size > max_video_bytes:
                    continue
                url = "/media/%s/%s" % (key, rel_url_path)
                files.append(url)
                continue

    _media_cache[key] = {
        "files": files[:],
        "mtime": max_mt,
        "scanned_at": time.time(),
    }

    random.shuffle(files)
    return files
