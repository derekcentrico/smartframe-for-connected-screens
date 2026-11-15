"""
Media listing, image caching / resizing, and HEIC support.
"""

import logging
import math
import os
import random
import tempfile
import threading
import time

from PIL import Image, ImageOps

from .config import CACHE_BASE, ENABLE_HEIC, ENABLE_VIDEOS, MAX_VIDEO_MB, MEDIA_BASE
from .faces import favorites_snapshot, focus_for_cache

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

IMAGE_EXTS = (".jpg", ".jpeg", ".png") + ((".heic", ".heif") if HEIC_SUPPORTED else ())

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


def _cache_path(frame_name: str, rel_path: str) -> str:
    """Return full path to cached image for a given frame and relative source path."""
    cache_dir = os.path.join(
        CACHE_BASE, frame_name.lower(), os.path.dirname(rel_path)
    )
    base, _ext = os.path.splitext(os.path.basename(rel_path))
    return os.path.join(cache_dir, base + "_1080x1920.jpg")


def _safe_cache_path(frame_name: str, rel_path: str) -> str:
    """Like _cache_path, but create the cache subdirectory if needed."""
    path = _cache_path(frame_name, rel_path)
    cache_dir = os.path.dirname(path)
    if not os.path.isdir(cache_dir):
        try:
            os.makedirs(cache_dir, exist_ok=True)
        except OSError:
            logger.warning("Could not create cache subdirectory %s", cache_dir)
    return path


# One full-size decode at a time per process. Upload pre-caching, frame
# requests for new photos and the face worker all land here, and a 12 MP
# photo needs well over 100 MB while it is being decoded.
_decode_slot = threading.Lock()

_CACHE_BOX = (1080, 1920)
# EXIF orientations that swap width and height.
_ROTATED = {5, 6, 7, 8}


def _cache_is_current(cache_path: str, full_src_path: str) -> bool:
    if not os.path.isfile(cache_path):
        return False
    return os.path.getmtime(full_src_path) <= os.path.getmtime(cache_path)


def _render_cache_image(full_src_path: str, cache_path: str) -> None:
    with Image.open(full_src_path) as src:
        box = _CACHE_BOX
        if src.getexif().get(0x0112) in _ROTATED:
            box = (_CACHE_BOX[1], _CACHE_BOX[0])
        # JPEGs decode straight to the smallest DCT scale that still covers
        # the final size; shrinking before rotating avoids any full-size copy.
        scale = min(1.0, box[0] / src.width, box[1] / src.height)
        src.draft(src.mode, (math.ceil(src.width * scale), math.ceil(src.height * scale)))
        if src.mode not in ("RGB", "RGBA", "L", "CMYK"):
            src = src.convert("RGB")
        src.thumbnail(box, Image.LANCZOS)
        im = ImageOps.exif_transpose(src).convert("RGB")
    # Write then rename: the face worker and request threads both build
    # cache images, and a reader must never see a half-written JPEG.
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(cache_path), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            im.save(fh, "JPEG", quality=72, optimize=True, progressive=True)
        os.chmod(tmp, 0o644)
        os.replace(tmp, cache_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get_cached_image(frame_name: str, full_src_path: str, rel_path: str):
    """Return absolute path to a resized 1080x1920 image, creating it if needed.

    Returns ``None`` if the image cannot be processed.
    """
    cache_path = _safe_cache_path(frame_name, rel_path)
    try:
        if _cache_is_current(cache_path, full_src_path):
            return cache_path
    except OSError:
        return None

    with _decode_slot:
        try:
            # Another thread may have built it while this one waited.
            if _cache_is_current(cache_path, full_src_path):
                return cache_path
            _render_cache_image(full_src_path, cache_path)
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
                max_mt = max(max_mt, mt)
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

    image_exts = IMAGE_EXTS
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
                url = f"/img/{key}/{rel_url_path}"
                files.append(url)
                continue

            if ENABLE_VIDEOS and lower.endswith(video_exts):
                try:
                    size = os.path.getsize(full_path)
                except OSError:
                    continue
                if size > max_video_bytes:
                    continue
                url = f"/media/{key}/{rel_url_path}"
                files.append(url)
                continue

    _media_cache[key] = {
        "files": files[:],
        "mtime": max_mt,
        "scanned_at": time.time(),
    }

    random.shuffle(files)
    return files


def focus_map(frame_name: str) -> dict:
    """Map each image URL for a frame to its face focus boxes, where known."""
    key = frame_name.lower()
    prefix = f"/img/{key}/"
    out = {}
    favorites = favorites_snapshot()
    for url in list_media(frame_name):
        if not url.startswith(prefix):
            continue
        focus = focus_for_cache(_cache_path(key, url[len(prefix):]), favorites)
        if focus:
            out[url] = focus
    return out
