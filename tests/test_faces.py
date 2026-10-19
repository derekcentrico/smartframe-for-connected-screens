import json
import os
import random
import shutil

import pytest
from PIL import Image

from app import faces, media
from app.config import CACHE_BASE, MEDIA_BASE


def _face(x, y, w, h, score=0.95, emb=None):
    return {"b": [x, y, w, h], "s": score, "e": emb}


def _person(seed, dim=128):
    rnd = random.Random(seed)
    return [rnd.gauss(0, 1) for _ in range(dim)]


def _jitter(vec, seed, amount=0.15):
    rnd = random.Random(seed)
    return [v + rnd.gauss(0, amount) for v in vec]


@pytest.fixture(autouse=True)
def clean_dirs():
    for d in (MEDIA_BASE, CACHE_BASE):
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
    faces._focus_cache.clear()
    faces._favorites.update({"mtime": None, "centroids": []})
    media._media_cache.clear()
    yield


def test_clear_faces_drops_background_and_low_confidence():
    fs = [
        _face(0.4, 0.3, 0.2, 0.12),           # subject
        _face(0.1, 0.1, 0.05, 0.03),          # small background face
        _face(0.7, 0.3, 0.2, 0.12, score=0.75),  # unsure detection
    ]
    kept = faces.clear_faces(fs, 1080, 1920)
    assert kept == [fs[0]]


def test_clear_faces_keeps_similar_sized_group():
    fs = [_face(0.1 * i, 0.4, 0.08, 0.05) for i in range(5)]
    assert len(faces.clear_faces(fs, 1080, 1920)) == 5


def test_union_box_pads_and_clamps():
    box = faces.union_box([_face(0.0, 0.0, 0.2, 0.2), _face(0.7, 0.5, 0.2, 0.2)])
    x, y, w, h = box
    assert x == 0.0 and y == 0.0
    assert x + w == pytest.approx(0.94)
    assert y + h == pytest.approx(0.75)
    assert faces.union_box([]) is None


def test_focus_marks_only_favorite_faces():
    fav = _person(1)
    other = _person(2)
    data = {"v": faces.SIDECAR_VERSION, "w": 1080, "h": 1920, "faces": [
        _face(0.1, 0.3, 0.15, 0.08, emb=_jitter(fav, 5)),
        _face(0.7, 0.3, 0.15, 0.08, emb=other),
    ]}
    focus = faces.focus_from_sidecar(data, [fav])
    assert focus["a"][0] < 0.1 and focus["a"][0] + focus["a"][2] > 0.85
    assert focus["f"][0] + focus["f"][2] < 0.5
    assert faces.focus_from_sidecar(data, [])["f"] is None


def test_small_faces_never_count_as_favorite():
    fav = _person(1)
    tiny = _face(0.1, 0.3, 0.02, 0.02, emb=fav)  # ~38 px tall, below EMBED_MIN_PX
    assert not faces.is_favorite(tiny, 1920, [fav])


def test_focus_rejects_unknown_sidecar_version():
    assert faces.focus_from_sidecar({"v": 999, "faces": []}, []) is None


def test_cluster_faces_finds_frequent_people():
    people = {name: _person(i) for i, name in enumerate(["a", "b", "c"])}
    entries = []
    counts = {"a": 12, "b": 8, "c": 2}
    for name, n in counts.items():
        for k in range(n):
            entries.append((f"{name}-{k}", _jitter(people[name], hash((name, k))), 100.0))
    clusters = faces.cluster_faces(entries, min_photos=3)
    assert [c["photos"] for c in clusters] == [12, 8]
    for c, name in zip(clusters, ["a", "b"]):
        assert faces._cosine(c["centroid"], people[name]) > 0.9


def test_cluster_faces_merges_split_seeds():
    # Two seeds of one person that only meet once both have grown.
    base = _person(7)
    entries = [(f"p{k}", _jitter(base, k, amount=0.9), 100.0) for k in range(40)]
    clusters = faces.cluster_faces(entries, min_photos=2)
    assert clusters and clusters[0]["photos"] >= 30


def test_cluster_faces_empty():
    assert faces.cluster_faces([], 3) == []


class _FakeModels:
    def __init__(self, emb):
        self.emb = emb
        self.calls = 0

    def analyze(self, path):
        self.calls += 1
        w, h = Image.open(path).size
        return {"v": faces.SIDECAR_VERSION, "w": w, "h": h,
                "faces": [_face(0.6, 0.1, 0.3, 0.2, emb=self.emb)]}


def _add_photo(frame, name, size=(1080, 1920)):
    folder = os.path.join(MEDIA_BASE, frame)
    os.makedirs(folder, exist_ok=True)
    Image.new("RGB", size, "gray").save(os.path.join(folder, name))


def test_scan_once_writes_sidecars_and_favorites_once():
    for i in range(4):
        _add_photo("upstairs", f"p{i}.jpg")
    models = _FakeModels(_person(3))

    written = faces.scan_once(models, media.get_cached_image, media.IMAGE_EXTS)
    assert written == 4
    with open(faces.FAVORITES_FILE) as fh:
        fav = json.load(fh)
    assert [c["photos"] for c in fav["clusters"]] == [4]

    assert faces.scan_once(models, media.get_cached_image, media.IMAGE_EXTS) == 0
    assert models.calls == 4


def test_focus_map_endpoint_and_frame_page():
    from app import create_app

    _add_photo("upstairs", "p0.jpg")
    _add_photo("upstairs", "p1.jpg")
    faces.scan_once(_FakeModels(_person(3)), media.get_cached_image, media.IMAGE_EXTS)

    client = create_app().test_client()
    data = client.get("/focus_map/upstairs").get_json()
    assert set(data) == {"/img/upstairs/p0.jpg", "/img/upstairs/p1.jpg"}
    box = data["/img/upstairs/p0.jpg"]["a"]
    assert box[0] == pytest.approx(0.54) and box[1] == pytest.approx(0.03)

    page = client.get("/frame/upstairs").get_data(as_text=True)
    assert "var FOCUS" in page and "/img/upstairs/p0.jpg" in page
    assert client.get("/focus_map/bad..name").status_code == 400


def test_focus_map_without_sidecars_is_empty():
    from app import create_app

    _add_photo("upstairs", "p0.jpg")
    client = create_app().test_client()
    assert client.get("/focus_map/upstairs").get_json() == {}


def test_deleted_photos_drop_out_of_clusters():
    for i in range(4):
        _add_photo("upstairs", f"p{i}.jpg")
    models = _FakeModels(_person(3))
    faces._last_inventory = set()
    faces.scan_once(models, media.get_cached_image, media.IMAGE_EXTS)

    for i in range(2):
        os.remove(os.path.join(MEDIA_BASE, "upstairs", f"p{i}.jpg"))
    assert faces.scan_once(models, media.get_cached_image, media.IMAGE_EXTS) == 0
    with open(faces.FAVORITES_FILE) as fh:
        assert json.load(fh)["clusters"] == []  # 2 photos < min of 3


def test_scan_skips_hidden_media_dirs():
    _add_photo("@eaDir", "thumb.jpg")
    _add_photo(".stversions", "old.jpg")
    models = _FakeModels(_person(3))
    assert faces.scan_once(models, media.get_cached_image, media.IMAGE_EXTS) == 0
    assert models.calls == 0


def test_malformed_sidecar_is_reanalyzed():
    _add_photo("upstairs", "p0.jpg")
    models = _FakeModels(_person(3))
    faces.scan_once(models, media.get_cached_image, media.IMAGE_EXTS)
    side = faces.sidecar_path(media._cache_path("upstairs", "p0.jpg"))
    with open(side, "w") as fh:
        fh.write('{"v": 1, "faces": []}')
    assert faces.scan_once(models, media.get_cached_image, media.IMAGE_EXTS) == 1


def test_malformed_sidecar_does_not_break_frame_page():
    from app import create_app

    _add_photo("upstairs", "p0.jpg")
    faces.scan_once(_FakeModels(_person(3)), media.get_cached_image, media.IMAGE_EXTS)
    side = faces.sidecar_path(media._cache_path("upstairs", "p0.jpg"))
    with open(side, "w") as fh:
        fh.write('{"v": 1, "w": 10, "h": 10, "faces": [{"b": [1]}]}')
    with open(faces.FAVORITES_FILE, "w") as fh:
        fh.write('{"clusters": [{"nope": 1}]}')
    client = create_app().test_client()
    assert client.get("/frame/upstairs").status_code == 200
    assert client.get("/focus_map/upstairs").get_json() == {}


def test_cache_and_sidecar_files_are_world_readable():
    _add_photo("upstairs", "p0.jpg")
    faces.scan_once(_FakeModels(_person(3)), media.get_cached_image, media.IMAGE_EXTS)
    cache = media._cache_path("upstairs", "p0.jpg")
    for path in (cache, faces.sidecar_path(cache), faces.FAVORITES_FILE):
        assert os.stat(path).st_mode & 0o044 == 0o044
    assert not [f for f in os.listdir(os.path.dirname(cache)) if f.endswith(".tmp")]
