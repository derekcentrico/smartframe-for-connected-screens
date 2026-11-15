"""
Upload blueprint: file uploads with deduplication, ZIP extraction, and optional auth.
"""

import errno
import hashlib
import hmac
import logging
import os
import re
import tempfile
import time
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from functools import wraps

from flask import Blueprint, Response, jsonify, render_template, request
from werkzeug.utils import secure_filename

from .config import (
    MAX_UPLOAD_MB,
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

# Max decompressed size per ZIP member (50 MB), and per archive in total and
# entry count, so a small archive cannot inflate until the media disk is full.
_MAX_ZIP_MEMBER_BYTES = 50 * 1024 * 1024
_MAX_ZIP_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
_MAX_ZIP_ENTRIES = 5000
# zipfile loads and parses the whole central directory when an archive is
# opened, before any per-entry check can run. 5,000 entries with names up to
# about 150 characters fit; tiny entries cap out near 20,000.
_MAX_ZIP_DIRECTORY_BYTES = 1024 * 1024
# Methods whose readers stop inflating at the size requested.
_ZIP_METHODS = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}

# Background pre-caching. One thread: decodes are serialized in media anyway,
# and a queue of waiting threads only holds requests open longer.
_cache_pool = ThreadPoolExecutor(max_workers=1)

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


def _dir_signature(frame_dir: str) -> tuple:
    """Names and directory mtimes under *frame_dir*.

    Adding, removing or renaming a file changes it. Names are included because
    directory mtimes are only as fine as the kernel's clock tick.
    """
    sig = []
    for root, _dirs, files in os.walk(frame_dir):
        try:
            mtime = os.stat(root).st_mtime_ns
        except OSError:
            continue
        sig.append((root, mtime, hash(tuple(sorted(files)))))
    return tuple(sig)


def _get_existing_hashes(frame_dir: str) -> set:
    """Return a set of SHA-256 hashes for *frame_dir*, using a short-lived cache.

    The cache is dropped when the directory changes outside an upload, so a
    photo deleted a moment ago is not reported as a duplicate.
    """
    sig = _dir_signature(frame_dir)
    cached = _hash_cache.get(frame_dir)
    if (cached and time.time() - cached["built_at"] < _HASH_CACHE_TTL
            and cached["sig"] == sig):
        return cached["hashes"]

    hashes = _build_existing_hashes(frame_dir)
    _hash_cache[frame_dir] = {"hashes": hashes, "built_at": time.time(), "sig": sig}
    return hashes


def _note_own_writes(frame_dir: str) -> None:
    """Accept this upload's own changes; _save_file already added their hashes."""
    cached = _hash_cache.get(frame_dir)
    if cached:
        cached["sig"] = _dir_signature(frame_dir)


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

    Returns ``"saved"`` | ``"duplicate"`` | ``"unsupported"`` | ``"failed"``.
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
        return "failed"

    # Pre-cache images via bounded thread pool
    if ext in _IMAGE_EXTENSIONS:
        rel_path = os.path.relpath(dest_path, os.path.join(MEDIA_BASE, frame_name.lower()))
        _cache_pool.submit(get_cached_image, frame_name, dest_path, rel_path)

    return "saved"


class _DirectoryTooLarge(Exception):
    pass


class _CappedOpen:
    """File wrapper that refuses any single read over a cap while armed.

    ZipFile loads the whole central directory with one read of the size the
    end record gives, then parses it from memory. Capping reads while the
    archive is opened bounds that directory whichever end record (classic or
    ZIP64, wherever it sits) zipfile decides to trust. Member reads happen
    after it is disarmed.
    """

    def __init__(self, raw, cap: int):
        self._raw = raw
        self._cap = cap
        self.armed = True

    def read(self, n: int = -1) -> bytes:
        if not self.armed:
            return self._raw.read(n)
        want = self._cap + 1 if n is None or n < 0 or n > self._cap else n
        data = self._raw.read(want)
        if len(data) > self._cap:
            raise _DirectoryTooLarge
        return data

    def seek(self, *args):
        return self._raw.seek(*args)

    def tell(self) -> int:
        return self._raw.tell()

    def seekable(self) -> bool:
        return True


def _iter_zip_members(stream):
    """Yield ``(safe_name, data)`` for supported members of a ZIP archive.

    Members are decompressed one at a time from *stream*; ``(None, None)`` marks
    a skipped entry so the caller can count it.
    """
    capped = _CappedOpen(stream, _MAX_ZIP_DIRECTORY_BYTES)
    try:
        zf = zipfile.ZipFile(capped)
    except _DirectoryTooLarge:
        logger.warning("ZIP directory over %d bytes, skipping", _MAX_ZIP_DIRECTORY_BYTES)
        yield None, None
        return
    except (zipfile.BadZipFile, OSError, EOFError, ValueError, OverflowError):
        logger.warning("Bad ZIP file received", exc_info=True)
        yield None, None
        return
    capped.armed = False
    if len(zf.infolist()) > _MAX_ZIP_ENTRIES:
        logger.warning("ZIP has %d entries, over %d; skipping",
                       len(zf.infolist()), _MAX_ZIP_ENTRIES)
        zf.close()
        yield None, None
        return

    inflated = 0
    with zf:
        for info in zf.infolist():
            member = info.filename
            if info.is_dir():
                continue
            basename = os.path.basename(member)
            if (member.startswith("__MACOSX/") or basename == ".DS_Store"
                    or basename.startswith("._")):
                continue

            ext = os.path.splitext(basename)[1].lower()
            safe_name = secure_filename(basename)
            if ext not in _MEDIA_EXTENSIONS or not safe_name:
                yield None, None
                continue
            if info.compress_type not in _ZIP_METHODS:
                # zipfile's bzip2 and LZMA readers inflate a whole input chunk
                # with no output limit, so a tiny member can claim little and
                # still allocate a great deal. Phones and desktop tools write
                # stored or deflate.
                logger.warning("ZIP member %r uses compression method %d, skipping",
                               member, info.compress_type)
                yield None, None
                continue
            if info.file_size > _MAX_ZIP_MEMBER_BYTES:
                logger.warning("ZIP member %r too large (%d bytes), skipping",
                               member, info.file_size)
                yield None, None
                continue

            budget = _MAX_ZIP_TOTAL_BYTES - inflated
            if info.file_size > budget:
                logger.warning("ZIP inflates past %d bytes; ignoring the rest",
                               _MAX_ZIP_TOTAL_BYTES)
                yield None, None
                return

            try:
                with zf.open(info) as fh:
                    # Ask for no more than the declared size, already checked
                    # against both caps: zipfile inflates as much as a read
                    # requests, even past a size that then fails the CRC. A
                    # member larger than it claims is cut short and fails it.
                    member_data = fh.read(info.file_size + 1)
            except (OSError, EOFError, ValueError, zipfile.BadZipFile, RuntimeError,
                    NotImplementedError, zlib.error):
                # ValueError covers undecodable member names. The work spent
                # on a corrupt member is bounded by its declared size.
                logger.warning("Failed to read ZIP member %r", member, exc_info=True)
                inflated += info.file_size
                yield None, None
                continue
            inflated += len(member_data)
            if len(member_data) > budget:
                logger.warning("ZIP inflates past %d bytes; ignoring the rest",
                               _MAX_ZIP_TOTAL_BYTES)
                yield None, None
                return
            if len(member_data) > _MAX_ZIP_MEMBER_BYTES:
                logger.warning("ZIP member %r inflates past the cap, skipping", member)
                yield None, None
                continue
            yield safe_name, member_data
            member_data = None  # drop it before inflating the next member


def _iter_upload_items(files):
    """Yield ``(filename, data)`` per uploaded file or ZIP member, one at a time.

    ``(None, None)`` marks a skipped input.
    """
    for f in files:
        if not f or not f.filename:
            continue
        filename = secure_filename(f.filename)
        if not filename:
            continue
        ext = os.path.splitext(filename)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            yield None, None
            continue
        if ext == ".zip":
            # Werkzeug spools large parts to disk, so the archive is read
            # from there rather than copied into memory.
            yield from _iter_zip_members(f.stream)
            continue
        try:
            data = f.read()
        except OSError:
            logger.warning("Failed to read upload %s", filename, exc_info=True)
            continue
        yield filename, data
        data = None  # drop it before reading the next file


def _upload_targets(targets):
    """Return ``[(frame, real_dir, existing_hashes)]`` for valid target frames."""
    dests = []
    media_root = os.path.realpath(MEDIA_BASE)
    for t in targets:
        if not t:
            continue
        if not _SAFE_FRAME_RE.match(t):
            logger.warning("Rejected invalid frame name: %r", t)
            continue
        real_target = os.path.realpath(os.path.join(MEDIA_BASE, t.lower()))
        if os.path.commonpath([real_target, media_root]) != media_root:
            logger.warning("Path traversal blocked for frame: %r", t)
            continue
        try:
            os.makedirs(real_target, exist_ok=True)
        except OSError:
            logger.warning("Cannot create frame dir %s", real_target)
            continue
        dests.append((t, real_target, _get_existing_hashes(real_target)))
    return dests


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
    counts = {"saved": 0, "duplicates": 0, "skipped": 0, "failed": 0}

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
                total_failed = 0

                # One file in memory at a time: each is hashed, written to
                # every target, then dropped before the next is read.
                dests = _upload_targets(targets)
                try:
                    for filename, data in _iter_upload_items(files):
                        if data is None:
                            total_skipped += 1
                            continue
                        data_hash = _file_hash(data)
                        for t, real_target, existing_hashes in dests:
                            result = _save_file(
                                data, filename, real_target, existing_hashes, t,
                                data_hash=data_hash,
                            )
                            if result == "saved":
                                total_saved += 1
                            elif result == "duplicate":
                                total_dup += 1
                            elif result == "failed":
                                total_failed += 1
                            else:
                                total_skipped += 1
                        del data
                finally:
                    # The cached hash sets already hold what _save_file added,
                    # and the page sends one request per file, so they are kept.
                    # Files saved before an error must still show up.
                    for t, real_target, _hashes in dests:
                        _note_own_writes(real_target)
                        invalidate_media_cache(t)
                counts = {"saved": total_saved, "duplicates": total_dup,
                          "skipped": total_skipped, "failed": total_failed}

                if total_saved > 0 or total_dup > 0:
                    parts = [f"Uploaded {total_saved} file(s)."]
                    if total_dup:
                        parts.append(f"Skipped {total_dup} duplicate(s).")
                    if total_skipped:
                        parts.append(f"Skipped {total_skipped} unsupported file(s).")
                    if total_failed:
                        parts.append(f"Could not save {total_failed} file(s).")
                        is_error = True
                    message = " ".join(parts)
                elif total_failed:
                    is_error = True
                    message = f"Could not save {total_failed} file(s). Check disk space and permissions."
                else:
                    is_error = True
                    if total_skipped and not total_saved:
                        message = "Only supported image/video files are allowed."
                    else:
                        message = "Nothing was uploaded. Check file types and permissions."

    # Return JSON for XHR uploads, HTML for normal form submissions
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        status_code = 400 if is_error else 200
        return jsonify({"message": message, "error": is_error, **counts}), status_code

    return render_template(
        "upload.html",
        frames=frames,
        default_frame=default_frame,
        message=message,
        is_error=is_error,
        max_upload_mb=MAX_UPLOAD_MB,
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
