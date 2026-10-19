"""
Face detection and frequent-face clustering for face-aware cropping.

Each cached image gets a ``.faces.json`` sidecar with normalised face boxes
and SFace embeddings. One background worker per cache directory (elected via
flock, so several gunicorn workers and containers can share ./cache) fills
the sidecars and reclusters. Request handlers only read sidecars, so OpenCV
and the models are loaded in a single process.
"""

import fcntl
import json
import logging
import math
import os
import re
import tempfile
import threading
import time

from PIL import Image

from .config import (
    CACHE_BASE,
    ENABLE_FACES,
    FACE_MODEL_DIR,
    FACE_SCAN_INTERVAL,
    FAVORITE_MIN_PHOTOS,
    MEDIA_BASE,
)

logger = logging.getLogger(__name__)

SIDECAR_VERSION = 1
DETECT_MODEL = "face_detection_yunet_2023mar.onnx"
EMBED_MODEL = "face_recognition_sface_2021dec.onnx"
FAVORITES_FILE = os.path.join(CACHE_BASE, ".favorite_faces.json")
LOCK_FILE = os.path.join(CACHE_BASE, ".faces.lock")
_FRAME_DIR_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

DETECT_SCORE = 0.7
# Faces below these limits are treated as background people.
CLEAR_SCORE = 0.8
CLEAR_MIN_FRAC = 0.05   # face height vs image short side
CLEAR_REL_FRAC = 0.5    # face height vs largest face in the photo
# Profiles and blurry faces give poor embeddings that pull unrelated people
# into one cluster, so only sharp, reasonably large faces vote.
EMBED_MIN_SCORE = 0.9
EMBED_MIN_PX = 64
# SFace's published "same person" cosine threshold is 0.363. Greedy centroid
# clustering drifts at that level (children merge into one cluster), so stricter.
SAME_PERSON_SIM = 0.45
CLUSTER_MERGE_SIM = 0.55
# Detecting on the full 1080x1920 cache costs ~95 MB more resident memory and
# finds no extra clear faces; embeddings still use the full-size image.
DETECT_MAX_SIDE = 960
# Cache images are at most 1080x1920; anything bigger is not ours to decode.
MAX_ANALYZE_PIXELS = 1080 * 1920 * 2


def sidecar_path(cache_path: str) -> str:
    return os.path.splitext(cache_path)[0] + ".faces.json"


def _atomic_write_json(path: str, data) -> None:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, separators=(",", ":"))
        # mkstemp creates 0600; other containers sharing ./cache may run as another UID.
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: str):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# ---------------------
# Pure helpers (no OpenCV)
# ---------------------


def clear_faces(faces: list, width: int, height: int) -> list:
    """Drop low-confidence and background faces."""
    if not faces or not width or not height:
        return []
    short = min(width, height)

    # Boxes are normalised per axis; convert heights to a short-side fraction.
    def h_frac(f):
        return f["b"][3] * height / short

    biggest = max(h_frac(f) for f in faces)
    return [
        f for f in faces
        if f["s"] >= CLEAR_SCORE
        and h_frac(f) >= CLEAR_MIN_FRAC
        and h_frac(f) >= CLEAR_REL_FRAC * biggest
    ]


def _padded(box: list) -> list:
    """Grow a face box to cover hair and chin, clamped to the image."""
    x, y, w, h = box
    x0 = max(0.0, x - 0.2 * w)
    y0 = max(0.0, y - 0.35 * h)
    x1 = min(1.0, x + w + 0.2 * w)
    y1 = min(1.0, y + h + 0.25 * h)
    return [x0, y0, x1 - x0, y1 - y0]


def union_box(faces: list):
    """Return the padded bounding box of all faces, or None."""
    if not faces:
        return None
    boxes = [_padded(f["b"]) for f in faces]
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes)
    y1 = max(b[1] + b[3] for b in boxes)
    return [round(x0, 4), round(y0, 4), round(x1 - x0, 4), round(y1 - y0, 4)]


def _cosine(a: list, b: list) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if not na or not nb:
        return 0.0
    return dot / (na * nb)


def embedding_usable(face: dict, height: int) -> bool:
    return (bool(face.get("e")) and face["s"] >= EMBED_MIN_SCORE
            and face["b"][3] * height >= EMBED_MIN_PX)


def is_favorite(face: dict, height: int, centroids: list) -> bool:
    if not centroids or not embedding_usable(face, height):
        return False
    return any(_cosine(face["e"], c) >= SAME_PERSON_SIM for c in centroids)


def focus_from_sidecar(data: dict, centroids: list):
    """Return {"a": box of all clear faces, "f": box of favourites or None}."""
    if not data or data.get("v") != SIDECAR_VERSION:
        return None
    faces = clear_faces(data.get("faces", []), data.get("w", 0), data.get("h", 0))
    if not faces:
        return None
    favs = [f for f in faces if is_favorite(f, data.get("h", 0), centroids)]
    return {"a": union_box(faces), "f": union_box(favs)}


def cluster_faces(entries: list, min_photos: int) -> list:
    """Group face embeddings by person and return frequent clusters.

    *entries* is a list of (photo_id, embedding, face_height). Returns
    [{"centroid": [...], "photos": n}] for clusters seen in at least
    *min_photos* distinct photos, most frequent first.
    """
    import numpy as np

    if not entries:
        return []
    # Large faces first: their embeddings are the most reliable seeds.
    entries = sorted(entries, key=lambda e: -e[2])
    vecs = np.asarray([e[1] for e in entries], dtype=np.float32)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    vecs = vecs / norms

    sums: list = []
    photos: list = []
    cent = np.zeros((0, vecs.shape[1]), dtype=np.float32)
    for (photo_id, _emb, _h), v in zip(entries, vecs):
        if len(sums):
            sims = cent @ v
            best = int(np.argmax(sims))
            if sims[best] >= SAME_PERSON_SIM:
                sums[best] += v
                photos[best].add(photo_id)
                cent[best] = sums[best] / np.linalg.norm(sums[best])
                continue
        sums.append(v.copy())
        photos.append({photo_id})
        cent = np.vstack([cent, v[None, :]])

    # Greedy assignment can split one person across seeds; merge close pairs.
    clusters = [[s, p] for s, p in zip(sums, photos)]
    merged = True
    while merged:
        merged = False
        big = [c for c in clusters if len(c[1]) >= 2]
        if len(big) < 2:
            break
        m = np.asarray([c[0] / np.linalg.norm(c[0]) for c in big])
        sims = m @ m.T
        np.fill_diagonal(sims, -1.0)
        i, j = np.unravel_index(int(np.argmax(sims)), sims.shape)
        if sims[i, j] >= CLUSTER_MERGE_SIM:
            a, b = big[i], big[j]
            a[0] = a[0] + b[0]
            a[1] = a[1] | b[1]
            clusters = [c for c in clusters if c is not b]
            merged = True

    out = [
        {"centroid": [round(float(x), 5) for x in c[0] / np.linalg.norm(c[0])],
         "photos": len(c[1])}
        for c in clusters if len(c[1]) >= min_photos
    ]
    out.sort(key=lambda c: -c["photos"])
    return out


# ---------------------
# Read side (request handlers)
# ---------------------

_focus_cache: dict = {}  # sidecar path -> (mtime, favorites stamp, focus)
_focus_lock = threading.Lock()
_favorites: dict = {"mtime": None, "centroids": []}


def _favorite_centroids():
    """Return (stamp, centroids), reloading the favourites file on change."""
    try:
        mtime = os.path.getmtime(FAVORITES_FILE)
    except OSError:
        return None, []
    if mtime != _favorites["mtime"]:
        data = _read_json(FAVORITES_FILE)
        try:
            centroids = [c["centroid"] for c in data["clusters"]]
        except (KeyError, TypeError):
            centroids = []
        _favorites["centroids"] = centroids
        _favorites["mtime"] = mtime
    return _favorites["mtime"], _favorites["centroids"]


def favorites_snapshot():
    """Return (stamp, centroids); call once per request, not per image."""
    with _focus_lock:
        return _favorite_centroids()


def focus_for_cache(cache_path: str, favorites=None):
    """Return the focus dict for a cached image, or None if unknown."""
    if not ENABLE_FACES:
        return None
    side = sidecar_path(cache_path)
    try:
        mtime = os.path.getmtime(side)
    except OSError:
        return None
    stamp, centroids = favorites if favorites is not None else favorites_snapshot()
    with _focus_lock:
        hit = _focus_cache.get(side)
        if hit and hit[0] == mtime and hit[1] == stamp:
            return hit[2]
    try:
        focus = focus_from_sidecar(_read_json(side), centroids)
    except (KeyError, IndexError, TypeError, ValueError):
        logger.warning("Ignoring malformed face sidecar %s", side)
        focus = None
    with _focus_lock:
        _focus_cache[side] = (mtime, stamp, focus)
    return focus


# ---------------------
# Worker side (OpenCV)
# ---------------------


class _Models:
    def __init__(self, model_dir: str):
        import cv2

        for name in (DETECT_MODEL, EMBED_MODEL):
            if not os.path.isfile(os.path.join(model_dir, name)):
                raise FileNotFoundError(os.path.join(model_dir, name))
        cv2.setNumThreads(1)
        self.cv2 = cv2
        self.det = cv2.FaceDetectorYN.create(
            os.path.join(model_dir, DETECT_MODEL), "", (320, 320), DETECT_SCORE, 0.3, 5000
        )
        self.rec = cv2.FaceRecognizerSF.create(os.path.join(model_dir, EMBED_MODEL), "")

    def analyze(self, image_path: str):
        try:
            with Image.open(image_path) as probe:
                if probe.width * probe.height > MAX_ANALYZE_PIXELS:
                    logger.warning("Skipping oversized image %s", image_path)
                    return None
        except OSError:
            return None
        im = self.cv2.imread(image_path)
        if im is None:
            return None
        h, w = im.shape[:2]
        scale = min(1.0, DETECT_MAX_SIDE / max(w, h))
        small = im
        if scale < 1.0:
            small = self.cv2.resize(im, (round(w * scale), round(h * scale)),
                                    interpolation=self.cv2.INTER_AREA)
        self.det.setInputSize((small.shape[1], small.shape[0]))
        _ok, rows = self.det.detect(small)
        faces = []
        for r in rows if rows is not None else []:
            r = r.copy()
            r[:14] /= scale  # box and landmarks back to full-size pixels
            x, y, bw, bh = (float(v) for v in r[:4])
            x0, y0 = max(0.0, x), max(0.0, y)
            x1, y1 = min(float(w), x + bw), min(float(h), y + bh)
            if x1 <= x0 or y1 <= y0:
                continue
            emb = self.rec.feature(self.rec.alignCrop(im, r)).flatten()
            faces.append({
                "b": [round(x0 / w, 4), round(y0 / h, 4),
                      round((x1 - x0) / w, 4), round((y1 - y0) / h, 4)],
                "s": round(float(r[14]), 3),
                "e": [round(float(v), 5) for v in emb],
            })
        return {"v": SIDECAR_VERSION, "w": w, "h": h, "faces": faces}


def _current_sidecar(cache_path: str, side: str):
    """Return the sidecar data if it is newer than the cache image and valid."""
    try:
        if os.path.getmtime(side) < os.path.getmtime(cache_path):
            return None
    except OSError:
        return None
    data = _read_json(side)
    if not isinstance(data, dict) or data.get("v") != SIDECAR_VERSION:
        return None
    if not data.get("w") or not data.get("h"):
        return None
    return data


_last_inventory: set = set()
_held_lock: list = []


def scan_once(models, get_cached_image, image_exts: tuple) -> int:
    """Fill missing sidecars and recluster. Returns sidecars written."""
    global _last_inventory
    written = 0
    entries = []
    inventory = set()
    if not os.path.isdir(MEDIA_BASE):
        return 0
    for frame in sorted(os.listdir(MEDIA_BASE)):
        folder = os.path.join(MEDIA_BASE, frame)
        # Skip NAS metadata dirs (.@__thumb, @eaDir) and anything not servable.
        if not _FRAME_DIR_RE.match(frame) or not os.path.isdir(folder):
            continue
        for root, _dirs, files in os.walk(folder):
            for name in files:
                if not name.lower().endswith(image_exts):
                    continue
                src = os.path.join(root, name)
                rel = os.path.relpath(src, folder)
                cache_path = get_cached_image(frame, src, rel)
                if cache_path is None:
                    continue
                side = sidecar_path(cache_path)
                data = _current_sidecar(cache_path, side)
                if data is None:
                    data = models.analyze(cache_path)
                    if data is None:
                        continue
                    try:
                        _atomic_write_json(side, data)
                        written += 1
                    except OSError:
                        logger.warning("Could not write %s", side, exc_info=True)
                        continue
                inventory.add(side)
                for f in clear_faces(data.get("faces", []), data["w"], data["h"]):
                    if embedding_usable(f, data["h"]):
                        entries.append((side, f["e"], f["b"][3] * data["h"]))

    # Deleted photos change the inventory too and must drop out of clusters.
    if written or inventory != _last_inventory or not os.path.isfile(FAVORITES_FILE):
        clusters = cluster_faces(entries, FAVORITE_MIN_PHOTOS)
        _atomic_write_json(FAVORITES_FILE, {"v": 1, "built": int(time.time()),
                                            "clusters": clusters})
        logger.info("Face scan: %d new sidecars, %d clear faces, %d frequent people",
                    written, len(entries), len(clusters))
    _last_inventory = inventory
    return written


def _worker_loop(get_cached_image, image_exts: tuple):
    lock_fh = None
    try:
        os.makedirs(CACHE_BASE, exist_ok=True)
    except OSError:
        logger.warning("Cannot create %s; face-aware cropping disabled", CACHE_BASE)
        return
    while lock_fh is None:
        try:
            # Held open for the life of the process; closing it drops the lock.
            fh = open(LOCK_FILE, "a")  # noqa: SIM115
        except OSError:
            logger.warning("Face worker cannot open %s; disabled", LOCK_FILE, exc_info=True)
            return
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_fh = fh
        except BlockingIOError:
            fh.close()
            time.sleep(FACE_SCAN_INTERVAL)
        except OSError:
            fh.close()
            logger.warning("Face worker cannot lock %s; disabled", LOCK_FILE, exc_info=True)
            return
    # Keep the lock even if the models fail to load, so the other workers
    # stay idle instead of retrying and logging the same failure forever.
    _held_lock.append(lock_fh)

    try:
        import cv2
    except ImportError:
        logger.warning("OpenCV not installed; face-aware cropping disabled")
        return
    try:
        models = _Models(FACE_MODEL_DIR)
    except (OSError, cv2.error) as exc:
        logger.warning("Face models unavailable (%s); face-aware cropping disabled", exc)
        return

    logger.info("Face worker started (pid %d)", os.getpid())
    while True:
        try:
            scan_once(models, get_cached_image, image_exts)
        except Exception:
            logger.exception("Face scan failed")
        time.sleep(FACE_SCAN_INTERVAL)


def start_worker(get_cached_image, image_exts: tuple) -> None:
    if not ENABLE_FACES:
        return
    t = threading.Thread(
        target=_worker_loop, args=(get_cached_image, image_exts),
        name="face-worker", daemon=True,
    )
    t.start()
