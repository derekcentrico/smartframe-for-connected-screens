"""
Upload blueprint: file uploads with deduplication, ZIP extraction, and optional auth.
"""

import errno
import hashlib
import hmac
import io
import logging
import os
import re
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from functools import wraps

from flask import Blueprint, Response, jsonify, render_template, request
from werkzeug.utils import secure_filename

from .config import (
    MEDIA_BASE,
    UPLOAD_AUTH_ENABLED,
    UPLOAD_PASSWORD,
    UPLOAD_USERNAME,
)
from .media import HEIC_SUPPORTED, get_cached_image, invalidate_media_cache

logger = logging.getLogger(__name__)

upload_bp = Blueprint("upload", __name__)

# Only allow alphanumeric, underscore, and hyphen in frame names.
_SAFE_FRAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# Max decompressed size per ZIP member (50 MB).
_MAX_ZIP_MEMBER_BYTES = 50 * 1024 * 1024

# Bounded thread pool for background image caching (prevents thread exhaustion).
_cache_pool = ThreadPoolExecutor(max_workers=4)

# Extensions accepted for direct upload / ZIP member extraction
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
if HEIC_SUPPORTED:
    _IMAGE_EXTENSIONS |= {".heic", ".heif"}

_VIDEO_EXTENSIONS = {".mp4"}
_MEDIA_EXTENSIONS = _IMAGE_EXTENSIONS | _VIDEO_EXTENSIONS
ALLOWED_EXTENSIONS = _MEDIA_EXTENSIONS | {".zip"}

# ---------------------
# Persistent hash cache
# ---------------------
# Avoids rehashing every file in a frame directory on every upload request.
# Keys: frame directory path -> {"hashes": set, "built_at": float}
_hash_cache: dict = {}
_HASH_CACHE_TTL = 60  # seconds


def _get_existing_hashes(frame_dir: str) -> set:
    """Return a set of SHA-256 hashes for *frame_dir*, using a short-lived cache."""
    cached = _hash_cache.get(frame_dir)
    if cached and time.time() - cached["built_at"] < _HASH_CACHE_TTL:
        return cached["hashes"]

    hashes = _build_existing_hashes(frame_dir)
    _hash_cache[frame_dir] = {"hashes": hashes, "built_at": time.time()}
    return hashes


def _invalidate_hash_cache(frame_dir: str):
    _hash_cache.pop(frame_dir, None)


# ---------------------
# Optional basic auth
# ---------------------


def _check_auth(username, password):
    if not UPLOAD_USERNAME or not UPLOAD_PASSWORD:
        return False
    user_ok = hmac.compare_digest(username, UPLOAD_USERNAME)
    pass_ok = hmac.compare_digest(password, UPLOAD_PASSWORD)
    return user_ok and pass_ok


def _requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not UPLOAD_AUTH_ENABLED:
            return f(*args, **kwargs)
        auth = request.authorization
        if not auth or not _check_auth(auth.username, auth.password):
            return Response(
                "Authentication required.", 401,
                {"WWW-Authenticate": 'Basic realm="SmartFrame Upload"'},
            )
        return f(*args, **kwargs)
    return decorated


# ---------------------
# Deduplication helpers
# ---------------------


def _file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_hash_from_path(filepath: str) -> str:
    """Compute SHA-256 of a file using chunked reads (memory-safe)."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _build_existing_hashes(frame_dir: str) -> set:
    """Walk *frame_dir* and return a set of SHA-256 hashes of all existing files."""
    hashes: set = set()
    for root, _, files in os.walk(frame_dir):
        for name in files:
            try:
                hashes.add(_file_hash_from_path(os.path.join(root, name)))
            except OSError:
                pass
    return hashes


# ---------------------
# Save helpers
# ---------------------


def _save_file(data: bytes, filename: str, target_dir: str,
               existing_hashes: set, frame_name: str, data_hash: str = ""):
    """
    Save *data* as *filename* into *target_dir*.

    Returns ``"saved"`` | ``"duplicate"`` | ``"unsupported"``.
    Handles collision when filename exists but hash differs.
    *data_hash* may be supplied to avoid recomputing when the caller already has it.
    """
    ext = os.path.splitext(filename)[1].lower()
    if ext not in _MEDIA_EXTENSIONS:
        return "unsupported"

    h = data_hash or _file_hash(data)
    if h in existing_hashes:
        return "duplicate"

    dest_path = os.path.join(target_dir, filename)

    # Handle filename collision (same name, different content)
    if os.path.exists(dest_path):
        base, extension = os.path.splitext(filename)
        dest_path = os.path.join(target_dir, f"{base}_{h[:8]}{extension}")

    try:
        # Atomic write: temp file + rename prevents partial/corrupted files on crash
        fd, tmp_path = tempfile.mkstemp(dir=target_dir, suffix=".tmp")
        closed = False
        try:
            os.write(fd, data)
            os.fsync(fd)
            os.close(fd)
            closed = True
            os.rename(tmp_path, dest_path)
            os.chmod(dest_path, 0o644)
        except BaseException:
            if not closed:
                os.close(fd)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        existing_hashes.add(h)
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            logger.exception("Disk full writing %s", dest_path)
        else:
            logger.warning("Failed to write uploaded file %s", dest_path, exc_info=True)
        return "unsupported"

    # Pre-cache images via bounded thread pool
    if ext in _IMAGE_EXTENSIONS:
        rel_path = os.path.relpath(dest_path, os.path.join(MEDIA_BASE, frame_name.lower()))
        _cache_pool.submit(get_cached_image, frame_name, dest_path, rel_path)

    return "saved"


def _extract_zip_members(data: bytes):
    """Extract supported members from a ZIP archive.

    Returns a list of ``(safe_name, member_data)`` tuples ready for saving,
    plus a count of skipped entries.  The ZIP is decompressed only once.
    """
    members = []
    skipped = 0

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        logger.warning("Bad ZIP file received")
        return members, 1

    for member in zf.namelist():
        if member.endswith("/"):
            continue
        basename = os.path.basename(member)
        if (member.startswith("__MACOSX/") or basename == ".DS_Store"
                or basename.startswith("._")):
            continue

        ext = os.path.splitext(basename)[1].lower()
        if ext not in _MEDIA_EXTENSIONS:
            skipped += 1
            continue

        try:
            info = zf.getinfo(member)
            if info.file_size > _MAX_ZIP_MEMBER_BYTES:
                logger.warning("ZIP member %s too large (%d bytes), skipping",
                               member, info.file_size)
                skipped += 1
                continue
        except KeyError:
            skipped += 1
            continue

        try:
            member_data = zf.read(member)
        except Exception:
            logger.warning("Failed to read ZIP member %s", member, exc_info=True)
            skipped += 1
            continue

        safe_name = secure_filename(basename)
        if not safe_name:
            skipped += 1
            continue

        members.append((safe_name, member_data))

    return members, skipped


# ---------------------
# Frame listing
# ---------------------


def _list_frames():
    """Return a sorted list of existing frame directory names under MEDIA_BASE."""
    frames = []
    if os.path.isdir(MEDIA_BASE):
        for entry in os.scandir(MEDIA_BASE):
            if entry.is_dir():
                frames.append(entry.name)
    return sorted(frames)


# ---------------------
# Upload handler
# ---------------------


def _handle_upload(frame_name_from_path=None):
    frames = _list_frames()
    message = ""
    is_error = False
    default_frame = frame_name_from_path

    if request.method == "POST":
        target = request.form.get("frame") or frame_name_from_path
        if not target:
            is_error = True
            message = "No frame selected."
        else:
            if target == "_all":
                targets = frames[:]
            else:
                targets = [target]

            files = request.files.getlist("files")
            if not files or not any(f and f.filename for f in files):
                is_error = True
                message = "No files received."
            else:
                total_saved = 0
                total_dup = 0
                total_skipped = 0

                # Pre-read all file data and pre-hash.
                # ZIP archives are decompressed once here.
                prepared_items = []  # [(filename, ext, data, data_hash)]
                for f in files:
                    if not f or not f.filename:
                        continue
                    filename = secure_filename(f.filename)
                    if not filename:
                        continue
                    ext = os.path.splitext(filename)[1].lower()
                    if ext not in ALLOWED_EXTENSIONS:
                        total_skipped += 1
                        continue
                    try:
                        data = f.read()
                    except Exception:
                        logger.warning("Failed to read upload %s", filename, exc_info=True)
                        continue

                    if ext == ".zip":
                        members, zip_skipped = _extract_zip_members(data)
                        total_skipped += zip_skipped
                        for safe_name, member_data in members:
                            h = _file_hash(member_data)
                            prepared_items.append((safe_name,
                                                   os.path.splitext(safe_name)[1].lower(),
                                                   member_data, h))
                    else:
                        h = _file_hash(data)
                        prepared_items.append((filename, ext, data, h))

                # Validate and process each target frame.
                for t in targets:
                    if not t:
                        continue
                    if not _SAFE_FRAME_RE.match(t):
                        logger.warning("Rejected invalid frame name: %s", t)
                        continue
                    target_dir = os.path.join(MEDIA_BASE, t.lower())
                    real_target = os.path.realpath(target_dir)
                    if not real_target.startswith(os.path.realpath(MEDIA_BASE)):
                        logger.warning("Path traversal blocked for frame: %s", t)
                        continue
                    try:
                        os.makedirs(real_target, exist_ok=True)
                    except OSError:
                        logger.warning("Cannot create frame dir %s", real_target)
                        continue

                    existing_hashes = _get_existing_hashes(real_target)

                    for filename, ext, data, data_hash in prepared_items:
                        result = _save_file(
                            data, filename, real_target, existing_hashes, t,
                            data_hash=data_hash,
                        )
                        if result == "saved":
                            total_saved += 1
                        elif result == "duplicate":
                            total_dup += 1
                        else:
                            total_skipped += 1

                    _invalidate_hash_cache(real_target)

                # Invalidate media cache for affected frames
                for t in targets:
                    invalidate_media_cache(t)

                if total_saved > 0 or total_dup > 0:
                    parts = [f"Uploaded {total_saved} file(s)."]
                    if total_dup:
                        parts.append(f"Skipped {total_dup} duplicate(s).")
                    if total_skipped:
                        parts.append(f"Skipped {total_skipped} unsupported file(s).")
                    message = " ".join(parts)
                else:
                    is_error = True
                    if total_skipped and not total_saved:
                        message = "Only supported image/video files are allowed."
                    else:
                        message = "Nothing was uploaded. Check file types and permissions."

    # Return JSON for XHR uploads, HTML for normal form submissions
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        status_code = 400 if is_error else 200
        return jsonify({"message": message, "error": is_error}), status_code

    return render_template(
        "upload.html",
        frames=frames,
        default_frame=default_frame,
        message=message,
        is_error=is_error,
    )


# ---------------------
# Routes
# ---------------------


@upload_bp.route("/upload", methods=["GET", "POST"])
@_requires_auth
def upload_root():
    """Upload UI - user chooses which frame to send files to."""
    return _handle_upload(frame_name_from_path=None)


@upload_bp.route("/upload/<frame_name>", methods=["GET", "POST"])
@_requires_auth
def upload_frame(frame_name):
    """Upload UI bound to a specific frame (e.g. /upload/upstairs)."""
    return _handle_upload(frame_name_from_path=frame_name)
